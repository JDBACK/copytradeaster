/** System Status screen (nested, Stage 3 part 2 P2.5).
 *
 * Read-only control-plane slice over the authoritative backend StatusDTO:
 * overall health, trust rail, and six diagnostic rows (Система,
 * Копирование, Aster, Разрешения, Позиции, Последняя проверка).  The last
 * successfully received DTO stays on screen: a failed read marks the view
 * transport-stale instead of destroying data, and a late request never
 * overwrites a newer one (sequence guard).  There is no manual refresh here —
 * the read heals on its own and the pull-down at the top of the shell forces
 * it.  The only action is ``open_bot``, which returns to the Telegram chat via
 * the existing ``closeMiniApp()``.
 */

import { useCallback, useEffect, useRef } from 'react'
import type { AuthContext } from '../api'
import { Brand, ErrorBox, Row } from '../components/ui'
import { describeError } from '../errors'
import {
  asterRowValue,
  copyRowValue,
  formatAge,
  lastCheckRowValue,
  overallValue,
  permissionsRowValue,
  needsPermissionReproof,
  positionsRowValue,
  reasonCopy,
  remediationOf,
  statusTrust,
  systemRowValue,
  TRANSPORT_STALE_WARNING,
} from '../systemStatus'
import { closeMiniApp, hapticImpact } from '../telegram'
import { formatTime } from '../format'
import { useAgeSeconds } from '../useAge'
import type { StatusDTO } from '../types'
import { railSpeaks } from '../live'
import { MiniAppResources } from '../resources'
import { useLiveSnapshot, useResource, useScreenResources, useTransportStale } from '../useResource'

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; dto: StatusDTO; transportStale: boolean; outageNote: boolean }
  | { kind: 'error'; code: string }

export function SystemStatusScreen({
  auth,
  accountMasked,
  resources: providedResources,
  foregroundSignal,
}: {
  auth: AuthContext
  accountMasked?: string | null
  resources?: MiniAppResources
  foregroundSignal?: number
}) {
  const resources = useScreenResources(providedResources, auth)
  // A healthy live boundary guarantees that the shared follower store was
  // authoritatively refreshed. Use its cheap cached Status read in steady
  // state; explicit remediation below remains refresh=true and therefore also
  // rechecks signed permissions.
  const liveCacheReady = resources.getLiveSnapshot().health === 'live'
  const resource = useResource({
    resources,
    key: 'status',
    active: true,
    strength: liveCacheReady ? 'weak' : 'strong',
    foregroundSignal,
  })
  const dto = resource.snapshot.data
  const transportUnhealthy = useTransportStale(resources, resource.snapshot.error)
  const railOwnsOutage = railSpeaks(useLiveSnapshot(resources), dto !== null)
  const transportStale = dto !== null && transportUnhealthy
  // One fact, one voice: the cached DTO keeps its honest stale verdict, but the
  // outage itself is not repeated here while the rail is already reporting it.
  const outageNote = transportStale && !railOwnsOutage
  // Verification evidence ages out on its own (the backend TTL is a minute), so
  // opening this screen re-proves it once instead of telling the user to ask
  // again. Bounded by the DTO itself: as soon as permissions read valid (or the
  // attempt failed and is described by the row) the effect stops re-firing.
  // again. Age counts too: a cached DTO can keep a green verdict with a stamp
  // from an hour ago, and the row would then read "Проверка: … · 25 мин назад".
  const permissionsUnproven = dto !== null && needsPermissionReproof(dto)
  const permissionsAutoChecked = useRef(false)
  useEffect(() => {
    if (!permissionsUnproven || permissionsAutoChecked.current) return
    // Once per open: evidence that ages out is re-proven by the app, not by a
    // button, and a failed attempt must not become a retry loop.
    permissionsAutoChecked.current = true
    void resource.refresh('strong').catch(() => {})
  }, [permissionsUnproven, resource])
  const load: LoadState = dto
    ? { kind: 'ready', dto, transportStale, outageNote }
    : resource.snapshot.error
      ? { kind: 'error', code: resource.snapshot.error }
      : { kind: 'loading' }

  const openBot = useCallback(() => {
    hapticImpact('light')
    closeMiniApp()
  }, [])

  const brand = (
    <Brand
      right={
        accountMasked ? (
          <span className="wallet-chip num">{accountMasked}</span>
        ) : (
          <span className="badge-testnet">Aster testnet</span>
        )
      }
    />
  )

  if (load.kind === 'loading') {
    return (
      <div className="screen">
        {brand}
        <h1 className="h1" style={{ marginTop: 4 }}>
          Статус системы
        </h1>
        <div className="skeleton" style={{ minHeight: 148 }} aria-label="Загрузка статуса" />
        <div className="skeleton" style={{ minHeight: 64 }} aria-label="Загрузка проверки" />
        <div className="skeleton" style={{ minHeight: 264 }} />
      </div>
    )
  }

  if (load.kind === 'error') {
    const copy = describeError(load.code)
    return (
      <div className="screen">
        {brand}
        <h1 className="h1" style={{ marginTop: 4 }}>
          Статус системы
        </h1>
        <ErrorBox title={copy.title} hint={copy.hint} />
      </div>
    )
  }

  return (
    <SystemStatusView
      dto={load.dto}
      transportStale={load.transportStale}
      outageNote={load.outageNote}
      accountMasked={accountMasked}
      onOpenBot={openBot}
      receivedAt={resource.snapshot.receivedAt}
    />
  )
}

/** Presentational status view over one authoritative DTO snapshot. */
export function SystemStatusView({
  dto,
  transportStale,
  outageNote = transportStale,
  accountMasked,
  onOpenBot,
  receivedAt = 0,
}: {
  dto: StatusDTO
  transportStale: boolean
  /** Whether this screen may repeat the outage in its own banner. */
  outageNote?: boolean
  accountMasked?: string | null
  onOpenBot: () => void
  /** When this DTO arrived: the backend age is a snapshot, and the row must stay
   * truthful while the screen is open. */
  receivedAt?: number
}) {
  const ageSeconds = useAgeSeconds(dto.age_seconds ?? null, receivedAt)
  // A transport failure keeps the previous DTO visible but must never let an
  // old ready verdict keep looking like fresh green health.
  const overall = transportStale ? { label: 'Данные устарели', tone: 'warning' as const } : overallValue(dto.state)
  const system = systemRowValue(dto.system_state)
  const copy = copyRowValue(dto.copy_state)
  const aster = asterRowValue(dto.aster_state)
  const permissions = permissionsRowValue(dto.permissions_state)
  const positions = positionsRowValue(dto.positions_count ?? null)
  const lastCheck = lastCheckRowValue(dto.last_check_at ?? null, ageSeconds)
  const reason = reasonCopy(dto.reason)
  const remediation = remediationOf(dto.remediation)
  const trust = statusTrust(dto)
  const account = dto.account_masked ?? accountMasked ?? null

  const amberNote = outageNote
    ? TRANSPORT_STALE_WARNING
    : dto.state === 'ready'
      ? null
      : (reason ?? `Последняя проверка: ${formatTime(dto.last_check_at)} · ${formatAge(ageSeconds)}`)

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
        Статус системы
      </h1>

      <section className="panel state-card" aria-label={`Состояние: ${overall.label}`}>
        <div className={`status-text tone-${overall.tone}`}>{overall.label}</div>
        <div className="freshness">
          <span
            className={`dot ${overall.tone === 'success' ? 'success' : overall.tone === 'muted' ? '' : overall.tone}`}
            aria-hidden
          />
          Проверка: {formatTime(dto.last_check_at)} · {formatAge(ageSeconds)}
        </div>
      </section>

      <section className="panel rail trust-strip" aria-label="Статус подключения">
        <ol style={{ display: 'contents', listStyle: 'none', padding: 0, margin: 0 }}>
          {trust.map((item) => (
            <li
              key={item.key}
              className={`rail-step${item.done ? ' done' : ''}`}
              aria-label={`${item.label}: ${item.done ? 'проверено' : 'не проверено'}`}
            >
              <div className="rail-circle">
                {item.done ? (
                  <svg width="18" height="18" viewBox="0 0 20 20" fill="none" aria-hidden>
                    <path
                      d="M4 10.5 8.2 14.5 16 6"
                      stroke="currentColor"
                      strokeWidth="2.2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                ) : (
                  <span className="trust-pending" aria-hidden />
                )}
              </div>
              <div className="rail-label" aria-hidden="true">
                {item.label}
              </div>
            </li>
          ))}
        </ol>
      </section>

      <section className="panel" aria-label="Диагностика">
        <Row label="Система" value={system.label} valueClass={`tone-${system.tone}`} />
        <Row label="Копирование" value={copy.label} valueClass={`tone-${copy.tone}`} />
        <Row label="Aster" value={aster.label} valueClass={`tone-${aster.tone}`} />
        <Row label="Разрешения" value={permissions.label} valueClass={`tone-${permissions.tone}`} />
        <Row label="Позиции" value={positions.label} valueClass={`tone-${positions.tone}`} />
        <Row label="Последняя проверка" value={lastCheck.label} valueClass={`tone-${lastCheck.tone}`} />
      </section>

      {amberNote && (
        <div className="stale-note" role="status">
          {amberNote}
        </div>
      )}

      <div className="btn-stack">
        {remediation === 'open_bot' && (
          <button type="button" className="btn btn-primary" onClick={onOpenBot}>
            Открыть бота
          </button>
        )}
      </div>
    </div>
  )
}
