/** Minimal Home (эталон 02-home-active, part-1 scope): состояние, риск,
 * открытые позиции, freshness и одна primary action — pause/resume. */

import { useCallback, useEffect, useRef, useState } from 'react'
import { HOME_REVALIDATE_MS, useWarmRevalidation, type WarmReason } from '../revalidate'
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
  invalidateSignal,
}: {
  auth: AuthContext
  onNotConnected: () => void
  active?: boolean
  /** Fired once when the screen first reaches ready (drives shell warmup). */
  onReady?: () => void
  /** Fired after a successful pause/resume so the shell can stale dependents. */
  onMutated?: () => void
  /** Shell-owned mutation counter; see useWarmRevalidation. */
  invalidateSignal?: number
}) {
  const [load, setLoad] = useState<LoadState>({ kind: 'loading' })
  const [acting, setActing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pendingConfirmation, setPendingConfirmation] = useState<ConfirmationDTO | null>(null)
  const seq = useRef(0)
  const abortRef = useRef<AbortController | null>(null)
  const readyNotifiedRef = useRef(false)

  const { markFresh } = useWarmRevalidation({
    active,
    ttlMs: HOME_REVALIDATE_MS,
    busy: load.kind === 'loading' || (load.kind === 'ready' && load.positionsPending),
    invalidateSignal,
    onRefresh: (reason: WarmReason) => {
      if (reason === 'retry') return fetchAll('foreground')
      return fetchAll('background')
    },
  })

  const fetchAll = useCallback(
    async (mode: 'foreground' | 'background', isUserTriggered = false) => {
      const my = ++seq.current
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      const signal = controller.signal
      if (isUserTriggered) setActing(true)
      try {
        const dashboard = await api.dashboard(auth, true, { signal })
        if (seq.current !== my) return
        if (dashboard.copy_state === 'empty') {
          onNotConnected()
          return
        }
        // Partial-ready: paint the dashboard before the cheap positions read,
        // keeping the previous preview (if any) instead of flashing.
        setLoad((prev) => ({
          kind: 'ready',
          dashboard,
          firstPosition: prev.kind === 'ready' ? prev.firstPosition : null,
          positionsPending: true,
        }))
        // Read positions cheaply from cached snapshot refreshed by dashboard.
        // The dashboard refresh above must stay strictly before this read —
        // never Promise.all: positions must see the refreshed snapshot.
        const posPage = await api.positions(auth, false, { signal }).catch((err: unknown) => {
          if (err instanceof ApiError && err.code === 'aborted') throw err
          return null
        })
        if (seq.current !== my) return
        markFresh()
        setLoad({
          kind: 'ready',
          dashboard,
          firstPosition: posPage && posPage.positions.length > 0 ? posPage.positions[0] : null,
          positionsPending: false,
        })
        if (mode === 'foreground') setError(null)
      } catch (err) {
        if (err instanceof ApiError && err.code === 'aborted') return
        if (seq.current !== my) return
        const code = err instanceof ApiError ? err.code : 'network_error'
        if (mode === 'background') {
          // Warm failure keeps the stale UI and only fills the error slot.
          setError(code)
        } else {
          setLoad({ kind: 'error', code })
        }
      } finally {
        if (isUserTriggered) setActing(false)
      }
    },
    [auth, onNotConnected, markFresh],
  )

  const refresh = useCallback(
    (isUserTriggered = false) => fetchAll('foreground', isUserTriggered),
    [fetchAll],
  )

  useEffect(() => {
    // Initial data fetch: fetchAll() only sets state after await (async), so
    // this is the standard external-system sync, not a cascading render.
    // oxlint-disable-next-line react/set-state-in-effect
    void fetchAll('foreground')
    return () => abortRef.current?.abort()
  }, [fetchAll])

  useEffect(() => {
    if (load.kind === 'ready' && !readyNotifiedRef.current) {
      readyNotifiedRef.current = true
      onReady?.()
    }
  }, [load, onReady])

  async function pause() {
    setActing(true)
    setError(null)
    try {
      const dashboard = await api.pause(auth)
      hapticImpact('medium')
      markFresh()
      setLoad((prev) =>
        prev.kind === 'ready'
          ? { ...prev, dashboard }
          : { kind: 'ready', dashboard, firstPosition: null, positionsPending: false },
      )
      onMutated?.()
    } catch (err) {
      hapticNotify('error')
      setError(err instanceof ApiError ? err.code : 'network_error')
    } finally {
      setActing(false)
    }
  }

  async function handleStartResume() {
    if (load.kind !== 'ready') return
    if (load.dashboard.system_state !== 'ready') {
      setError('stale_operation')
      return
    }
    setActing(true)
    setError(null)
    try {
      const pending = await api.beginConfirmation(auth, 'resume')
      setPendingConfirmation(pending)
    } catch (err) {
      hapticNotify('error')
      setError(err instanceof ApiError ? err.code : 'network_error')
    } finally {
      setActing(false)
    }
  }

  async function handleConfirmResume(confirmation: ConfirmationDTO) {
    setActing(true)
    setError(null)
    try {
      const dashboard = await api.resume(auth, confirmation)
      hapticNotify('success')
      setPendingConfirmation(null)
      markFresh()
      setLoad((prev) =>
        prev.kind === 'ready'
          ? { ...prev, dashboard }
          : { kind: 'ready', dashboard, firstPosition: null, positionsPending: false },
      )
      onMutated?.()
    } catch (err) {
      hapticNotify('error')
      setError(err instanceof ApiError ? err.code : 'network_error')
      setPendingConfirmation(null)
    } finally {
      setActing(false)
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
    dashboard.system_state === 'ready'
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
      {error && <ErrorBox {...describeError(error)} />}

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
          Система: {systemLabel(dashboard.system_state)} · Снимок:{' '}
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
          <button type="button" className="btn btn-primary" disabled={acting} onClick={() => void pause()}>
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
          <p className="footer-note">Пауза останавливает только новые входы — защита продолжит работать.</p>
        </div>
      )}

      {view.actionLabel === 'resume' && (
        <div className="btn-stack">
          <button
            type="button"
            className="btn btn-primary"
            disabled={acting || dashboard.system_state !== 'ready'}
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
            disabled={acting}
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
            disabled={acting}
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
            disabled={acting}
            onClick={() => void handleConfirmResume(pendingConfirmation)}
          >
            {acting ? 'Возобновляем…' : 'Подтвердить возобновление'}
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => setPendingConfirmation(null)}
            disabled={acting}
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
