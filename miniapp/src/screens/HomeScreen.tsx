/** Minimal Home (эталон 02-home-active, part-1 scope): состояние, риск,
 * открытые позиции, freshness и одна primary action — pause/resume. */

import { useCallback, useEffect, useRef, useState } from 'react'
import { HOME_REVALIDATE_MS } from '../revalidate'
import { api, ApiError, type AuthContext } from '../api'
import { describeError } from '../errors'
import { formatPercent, formatPnl, formatUsdt } from '../format'
import {
  resumeConfirmBody,
  RESUME_CONFIRM_TITLE,
  homeViewModel,
  snapshotLabel,
} from '../home'
import { hapticImpact, hapticNotify } from '../telegram'
import type { ConfirmationDTO, DashboardDTO } from '../types'
import { Brand, ErrorBox, Sheet } from '../components/ui'
import type { NestedScreen, RootTab } from '../navigation'
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
  onOpen,
  onTab,
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
  onOpen?: (screen: NestedScreen) => void
  onTab?: (tab: RootTab) => void
}) {
  const [acting, setActing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pendingConfirmation, setPendingConfirmation] = useState<ConfirmationDTO | null>(null)
  const actingRef = useRef(false)
  const readyNotifiedRef = useRef(false)
  const resources = useScreenResources(providedResources, auth)
  // Once the live rail is genuinely healthy, its update is already backed by
  // the process-local authoritative follower snapshot. Steady reads must then
  // consume that cache instead of launching another Aster REST refresh. This
  // also prevents a refresh=true request from fencing itself when its own
  // FollowerSnapshotStore publication reaches this client before HTTP settles.
  const liveCacheReady = resources.getLiveSnapshot().health === 'live'

  const dashboardResource = useResource({
    resources,
    key: 'dashboard',
    active,
    ttlMs: HOME_REVALIDATE_MS,
    strength: liveCacheReady ? 'weak' : 'strong',
    foregroundSignal,
  })
  const resourceDashboard = dashboardResource.snapshot.data
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
  const overview = dashboard.overview
  const pnl = formatPnl(overview?.unrealized_pnl_usdt)
  const stale = transportStale || overview?.freshness !== 'fresh'

  return (
    <div className="screen">
      <Brand
        right={
          dashboard.account_masked ? (
            <button type="button" className="wallet-chip num" onClick={() => onOpen?.({ kind: 'account-builder' })}>{dashboard.account_masked}</button>
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

      <button type="button" className="summary-status" onClick={() => onOpen?.({ kind: 'system-status' })}>
        <span className={`dot ${stale ? 'warning' : view.tone === 'active' ? 'success' : 'warning'}`} aria-hidden />
        <span>{transportStale ? 'Данные требуют проверки' : view.title}</span>
        <span className="summary-chevron" aria-hidden>›</span>
      </button>
      <section className="panel account-summary" aria-label="Сводка аккаунта">
        <h1 className="summary-label">PnL открытых позиций</h1>
        <button type="button" className={`summary-pnl num pnl-${pnl.tone}`} onClick={() => onTab?.('positions')}>
          {pnl.text}
        </button>
        <p className="freshness">По маркировочной цене · {stale ? 'данные требуют обновления' : 'обновлено'} {snapshotLabel(overview?.as_of)}</p>
        <button type="button" className="summary-row" onClick={() => onTab?.('positions')}><span>Открытые позиции</span><strong className="num">{overview?.open_count ?? '—'}</strong></button>
        <div className="summary-row"><span>Стоп подтверждён</span><strong>{stale ? 'Не обновлён' : `${overview?.protected_count ?? '—'} из ${overview?.open_count ?? '—'}`}</strong></div>
        <div className="summary-row"><span>Доступно для торговли</span><strong className="num">{formatUsdt(overview?.available_usdt)}</strong></div>
        <button type="button" className="summary-row" onClick={() => onOpen?.({ kind: 'risk' })}><span>Риск на сделку</span><strong className="num">{formatPercent(dashboard.risk_percent)}</strong></button>
      </section>
      {!overview && <p className="info-box">Получаем данные аккаунта</p>}
      {overview?.open_count === 0 && overview.freshness === 'fresh' && <p className="info-box">Нет открытых позиций. {dashboard.copy_state === 'active' ? 'Копирование включено. Открытые позиции появятся здесь.' : 'Новые входы на паузе.'}</p>}
      {overview?.open_count != null && overview.protected_count != null && overview.open_count > overview.protected_count && (
        <button type="button" className="position-warning" onClick={() => onOpen?.({ kind: 'trade-list', state: 'open', period: 'all', attention: true })}>
          У {overview.open_count - overview.protected_count} позиций стоп не подтверждён
        </button>
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
          {(transportStale || ownerMutationState === 'unknown') && (
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => void refresh(true)}
              disabled={acting || ownerActive}
            >
              Проверить состояние
            </button>
          )}
        </div>
      )}

      {view.actionLabel === null && (transportStale || ownerMutationState === 'unknown') && (
        <div className="btn-stack">
          <button
            type="button"
            className="btn btn-secondary"
            disabled={acting || ownerActive}
            onClick={() => void refresh(true)}
          >
            {acting ? 'Проверяем…' : 'Проверить состояние'}
          </button>
        </div>
      )}

      {pendingConfirmation && (
        <Sheet
          title={RESUME_CONFIRM_TITLE}
          body={resumeConfirmBody(pendingConfirmation.resume_context)}
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
