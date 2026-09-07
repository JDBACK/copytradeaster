/** Minimal Home (эталон 02-home-active, part-1 scope): состояние, риск,
 * открытые позиции, freshness и одна primary action — pause/resume. */

import { useCallback, useEffect, useRef, useState } from 'react'
import { HOME_REVALIDATE_MS } from '../revalidate'
import { api, ApiError, type AuthContext } from '../api'
import { describeError } from '../errors'
import { formatPercent } from '../format'
import {
  RESUME_CONFIRM_BODY,
  RESUME_CONFIRM_TITLE,
  homeViewModel,
  snapshotLabel,
  trustSummary,
} from '../home'
import { hapticImpact, hapticNotify } from '../telegram'
import type { ConfirmationDTO, DashboardDTO, PositionSummaryDTO } from '../types'
import { Brand, ErrorBox, Sheet } from '../components/ui'
import { PositionCard } from './PositionsScreen'
import { MiniAppResources } from '../resources'
import { useResource, useScreenResources } from '../useResource'

// Re-exported for existing importers (behavior tests); the value lives in
// the shared warm-activation primitive and doubles as the 30 s client floor
// for positions/settings (never below the server snapshot TTL).
export { HOME_REVALIDATE_MS } from '../revalidate'

type LoadState =
  | { kind: 'loading' }
  | {
      kind: 'ready'
      dashboard: DashboardDTO
      firstPosition: PositionSummaryDTO | null
      positionsPending: boolean
    }
  | { kind: 'error'; code: string }

export function HomeScreen({
  auth,
  onNotConnected,
  active = true,
  onReady,
  onMutated,
  resources: providedResources,
  foregroundSignal,
}: {
  auth: AuthContext
  onNotConnected: () => void
  active?: boolean
  /** Fired once when the screen first reaches ready (drives shell warmup). */
  onReady?: () => void
  /** Fired after a successful pause/resume so the shell can stale dependents. */
  onMutated?: () => void
  resources?: MiniAppResources
  foregroundSignal?: number
}) {
  const [acting, setActing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pendingConfirmation, setPendingConfirmation] = useState<ConfirmationDTO | null>(null)
  const actingRef = useRef(false)
  const readyNotifiedRef = useRef(false)
  const resources = useScreenResources(providedResources, auth)

  const dashboardResource = useResource({
    resources,
    key: 'dashboard',
    active,
    ttlMs: HOME_REVALIDATE_MS,
    strength: 'strong',
    foregroundSignal,
  })
  const positionsResource = useResource({
    resources,
    key: 'positions',
    enabled: dashboardResource.snapshot.data !== null,
    active,
    ttlMs: HOME_REVALIDATE_MS,
    strength: 'weak',
    foregroundSignal,
  })

  const resourceDashboard = dashboardResource.snapshot.data
  const positions = positionsResource.snapshot.data
  const transportStale =
    resourceDashboard !== null &&
    (dashboardResource.snapshot.stale || dashboardResource.snapshot.error !== null)
  const ownerMutationState = resources.mutationState('pause-resume')
  const ownerActing = ownerMutationState !== null
  const ownerActive = ownerMutationState === 'active'
  const ownerUnknownSince = resources.mutationUnknownSinceRevision('pause-resume')
  const load: LoadState = resourceDashboard
    ? {
        kind: 'ready',
        dashboard: resourceDashboard,
        firstPosition: positions?.positions[0] ?? null,
        positionsPending:
          positions === null && positionsResource.snapshot.error === null,
      }
    : dashboardResource.snapshot.error
      ? { kind: 'error', code: dashboardResource.snapshot.error }
      : { kind: 'loading' }

  useEffect(() => {
    if (ownerMutationState !== 'unknown' || ownerUnknownSince === null) return
    const snapshot = dashboardResource.snapshot
    if (
      snapshot.revision > ownerUnknownSince &&
      snapshot.data !== null &&
      snapshot.error === null &&
      !snapshot.stale &&
      snapshot.strength === 'strong'
    ) {
      resources.resolveUnknownMutation('pause-resume')
      // The authoritative late recovery closes the same mutation boundary as
      // an immediate pause/resume response. Positions are already invalidated
      // by accepted strong Dashboard publication; stale the remaining
      // dependents and notify Activity through the shell-owned generation.
      resources.invalidate(['status'])
      onMutated?.()
    }
  }, [dashboardResource.snapshot, onMutated, ownerMutationState, ownerUnknownSince, resources])

  const refresh = useCallback(
    async (isUserTriggered = false) => {
      if (isUserTriggered) setActing(true)
      setError(null)
      try {
        await dashboardResource.refresh('strong')
      } catch (err) {
        if (!(err instanceof ApiError && err.code === 'aborted')) {
          setError(err instanceof ApiError ? err.code : 'network_error')
        }
      } finally {
        if (isUserTriggered) setActing(false)
      }
    },
    [dashboardResource],
  )

  useEffect(() => {
    if (resourceDashboard?.copy_state === 'empty') onNotConnected()
  }, [resourceDashboard, onNotConnected])

  useEffect(() => {
    if (resourceDashboard && resourceDashboard.copy_state !== 'empty' && !readyNotifiedRef.current) {
      readyNotifiedRef.current = true
      onReady?.()
    }
  }, [resourceDashboard, onReady])

  useEffect(() => {
    // A keep-alive root may become hidden without unmounting. Confirmation
    // tokens are interaction state, not cached DTO data: do not leave a hidden
    // Sheet holding body overflow/focus listeners, and do not resurrect an old
    // confirmation when the user returns to Home.
    if (!active && pendingConfirmation !== null) {
      // oxlint-disable-next-line react/set-state-in-effect
      setPendingConfirmation(null)
    }
  }, [active, pendingConfirmation])

  async function pause() {
    if (actingRef.current) return
    const release = resources.acquireMutation('pause-resume')
    if (!release) return
    actingRef.current = true
    setActing(true)
    setError(null)
    try {
      const next = await api.pause(auth)
      hapticImpact('medium')
      resources.publishConfirmed('dashboard', next)
      resources.invalidate(['positions', 'status'])
      onMutated?.()
    } catch (err) {
      if (resources.handleInvalidSessionError(err)) return
      const code = err instanceof ApiError ? err.code : 'network_error'
      if (code === 'network_error' || code === 'timeout_error') {
        try {
          // Mutation outcome is ambiguous. Fence every Dashboard read that
          // began before the write boundary so refreshCurrent cannot accept a
          // pre-mutation strong flight as authoritative reconciliation.
          resources.invalidate(['dashboard'])
          const current = await resources.refreshCurrent('dashboard', 'strong')
          if (current.copy_state === 'paused') {
            resources.invalidate(['positions', 'status'])
            hapticNotify('success')
            onMutated?.()
            return
          }
        } catch (verifyErr) {
          if (resources.handleInvalidSessionError(verifyErr)) return
          // The write may have committed even though neither response reached
          // us. Retain typed uncertainty across remount until a later strong
          // Dashboard read proves current server state.
          resources.markMutationUnknown('pause-resume', 'pause')
        }
      }
      hapticNotify('error')
      setError(code)
    } finally {
      setActing(false)
      actingRef.current = false
      release()
    }
  }

  async function handleStartResume() {
    if (load.kind !== 'ready') return
    if (actingRef.current) return
    if (load.dashboard.system_state !== 'ready') {
      setError('stale_operation')
      return
    }
    actingRef.current = true
    setActing(true)
    setError(null)
    try {
      const pending = await api.beginConfirmation(auth, 'resume')
      setPendingConfirmation(pending)
    } catch (err) {
      if (resources.handleInvalidSessionError(err)) return
      hapticNotify('error')
      setError(err instanceof ApiError ? err.code : 'network_error')
    } finally {
      setActing(false)
      actingRef.current = false
    }
  }

  async function handleConfirmResume(confirmation: ConfirmationDTO) {
    if (actingRef.current) return
    const release = resources.acquireMutation('pause-resume')
    if (!release) return
    actingRef.current = true
    setActing(true)
    setError(null)
    try {
      const next = await api.resume(auth, confirmation)
      hapticNotify('success')
      setPendingConfirmation(null)
      resources.publishConfirmed('dashboard', next)
      resources.invalidate(['positions', 'status'])
      onMutated?.()
    } catch (err) {
      setPendingConfirmation(null)
      if (resources.handleInvalidSessionError(err)) return
      const code = err instanceof ApiError ? err.code : 'network_error'
      if (code === 'network_error' || code === 'timeout_error') {
        try {
          // A pre-confirmation strong Dashboard flight is not evidence of the
          // resume outcome. Fence it before performing authoritative recovery.
          resources.invalidate(['dashboard'])
          const current = await resources.refreshCurrent('dashboard', 'strong')
          if (current.copy_state === 'active') {
            resources.invalidate(['positions', 'status'])
            hapticNotify('success')
            onMutated?.()
            return
          }
        } catch (verifyErr) {
          if (resources.handleInvalidSessionError(verifyErr)) return
          // Confirmation tokens are single-use. Retain typed uncertainty until
          // a later strong Dashboard read proves current state; never replay.
          resources.markMutationUnknown('pause-resume', 'resume')
        }
      }
      hapticNotify('error')
      setError(code)
    } finally {
      setActing(false)
      actingRef.current = false
      release()
    }
  }

  if (load.kind === 'loading') {
    return (
      <div className="screen">
        <Brand right={<span className="badge-testnet">Aster testnet</span>} />
        <div className="skeleton" style={{ minHeight: 200 }} aria-label="Загрузка состояния" />
        <div className="metric-grid">
          <div className="skeleton" style={{ minHeight: 84 }} />
          <div className="skeleton" style={{ minHeight: 84 }} />
        </div>
      </div>
    )
  }

  if (load.kind === 'error') {
    const copy = describeError(load.code)
    return (
      <div className="screen">
        <Brand right={<span className="badge-testnet">Aster testnet</span>} />
        <ErrorBox title={copy.title} hint={copy.hint} />
        <div className="btn-stack">
          <button type="button" className="btn btn-secondary" onClick={() => void refresh()}>
            Обновить
          </button>
        </div>
      </div>
    )
  }

  const dashboard = load.dashboard
  const view = homeViewModel(dashboard)
  const trust = trustSummary(dashboard)
  const systemDot =
    transportStale
      ? 'warning'
      : dashboard.system_state === 'ready'
      ? 'success'
      : dashboard.system_state === 'stale'
        ? 'warning'
        : 'danger'

  return (
    <div className="screen">
      <Brand
        right={
          dashboard.account_masked ? (
            <span className="wallet-chip num">{dashboard.account_masked}</span>
          ) : (
            <span className="badge-testnet">Aster testnet</span>
          )
        }
      />

      {/* Error slot honesty: partial-ready paints over dashboard data without
          clearing a visible error, and a failed background revalidation only
          fills this slot — the stale ready UI underneath is never wiped. */}
      {(error ?? dashboardResource.snapshot.error) && (
        <ErrorBox {...describeError((error ?? dashboardResource.snapshot.error)!)} />
      )}

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
              <div className="rail-label" aria-hidden="true">{item.label}</div>
            </li>
          ))}
        </ol>
      </section>

      <section className="panel state-card">
        <div className="state-icon" aria-hidden>
          {view.tone === 'active' ? (
            <svg width="36" height="36" viewBox="0 0 24 24" fill="none">
              <path
                d="M5 12.5 9.5 17 19 7"
                stroke="var(--success)"
                strokeWidth="2.4"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          ) : view.tone === 'paused' ? (
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none">
              <rect x="6" y="5" width="4" height="14" rx="1.5" fill="var(--warning)" />
              <rect x="14" y="5" width="4" height="14" rx="1.5" fill="var(--warning)" />
            </svg>
          ) : (
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none">
              <circle cx="12" cy="12" r="9" stroke="var(--danger)" strokeWidth="2" />
              <path d="M12 8v5M12 16h.01" stroke="var(--danger)" strokeWidth="2.2" strokeLinecap="round" />
            </svg>
          )}
        </div>
        <h1 className="h1" style={{ margin: 0 }}>
          {view.title}
        </h1>
        <div
          className={`status-text ${
            view.tone === 'active' ? 'tone-success' : view.tone === 'paused' ? 'tone-warning' : 'tone-danger'
          }`}
        >
          {view.subtitle}
        </div>
        <div className="freshness">
          <span className={`dot ${systemDot}`} aria-hidden />
          Система: {transportStale ? 'данные требуют проверки' : systemLabel(dashboard.system_state)} · Снимок:{' '}
          {snapshotLabel(dashboard.snapshot_captured_at)}
        </div>
      </section>

      <div className="metric-grid">
        <div className="metric">
          <div className="metric-label">Риск на стоп</div>
          <div className="metric-value num">{formatPercent(dashboard.risk_percent)}</div>
        </div>
        <div className="metric">
          <div className="metric-label">Открытые позиции</div>
          <div className="metric-value num">{dashboard.open_positions ?? '—'}</div>
        </div>
      </div>

      {load.firstPosition ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div className="section-label" style={{ marginTop: 2 }}>
            Активная позиция
          </div>
          <PositionCard position={load.firstPosition} />
        </div>
      ) : (
        load.positionsPending && (
          <div className="skeleton" style={{ minHeight: 96 }} aria-label="Загрузка позиций" />
        )
      )}

      {view.actionLabel === 'pause' && (
        <div className="btn-stack">
          <button type="button" className="btn btn-primary" disabled={acting || ownerActing} onClick={() => void pause()}>
            {acting ? (
              'Ставим на паузу…'
            ) : (
              <>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
                  <rect x="6" y="4" width="4" height="16" rx="1.5" />
                  <rect x="14" y="4" width="4" height="16" rx="1.5" />
                </svg>
                Поставить на паузу
              </>
            )}
          </button>
          {ownerMutationState === 'unknown' && (
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => void refresh(true)}
              disabled={acting || ownerActive}
            >
              Обновить данные
            </button>
          )}
          <p className="footer-note">Пауза останавливает только новые входы — защита продолжит работать.</p>
        </div>
      )}

      {view.actionLabel === 'resume' && (
        <div className="btn-stack">
          <button
            type="button"
            className="btn btn-primary"
            disabled={acting || ownerActing || dashboard.system_state !== 'ready'}
            onClick={() => void handleStartResume()}
          >
            {acting ? (
              'Загрузка…'
            ) : (
              <>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
                  <path d="M7 5.5v13a1 1 0 0 0 1.5.86l11-6.5a1 1 0 0 0 0-1.72l-11-6.5A1 1 0 0 0 7 5.5Z" />
                </svg>
                Возобновить
              </>
            )}
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => void refresh(true)}
            disabled={acting || ownerActive}
          >
            Обновить данные
          </button>
        </div>
      )}

      {view.actionLabel === null && (
        <div className="btn-stack">
          <button
            type="button"
            className="btn btn-secondary"
            disabled={acting || ownerActive}
            onClick={() => void refresh(true)}
          >
            {acting ? 'Обновляем…' : 'Обновить данные'}
          </button>
        </div>
      )}

      {pendingConfirmation && (
        <Sheet
          title={RESUME_CONFIRM_TITLE}
          body={RESUME_CONFIRM_BODY}
          onClose={() => {
            if (!acting) setPendingConfirmation(null)
          }}
        >
          <button
            type="button"
            className="btn btn-primary"
            disabled={acting || ownerActing}
            onClick={() => void handleConfirmResume(pendingConfirmation)}
          >
            {acting ? 'Возобновляем…' : 'Подтвердить возобновление'}
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => setPendingConfirmation(null)}
            disabled={acting || ownerActing}
          >
            Отмена
          </button>
        </Sheet>
      )}
    </div>
  )
}

function systemLabel(state: DashboardDTO['system_state']): string {
  switch (state) {
    case 'ready':
      return 'готова'
    case 'stale':
      return 'данные устарели'
    case 'degraded':
      return 'деградация'
    case 'blocked':
      return 'заблокирована'
    default:
      return 'неизвестно'
  }
}
