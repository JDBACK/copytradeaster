/** Minimal Home (эталон 02-home-active, part-1 scope): состояние, риск,
 * открытые позиции, freshness и одна primary action — pause/resume. */

import { useCallback, useEffect, useState } from 'react'
import { api, ApiError, type AuthContext } from '../api'
import { describeError } from '../errors'
import { formatPercent, formatTime } from '../format'
import { RESUME_CONFIRM_BODY, RESUME_CONFIRM_TITLE, homeViewModel } from '../home'
import { hapticImpact, hapticNotify } from '../telegram'
import type { ConfirmationDTO, DashboardDTO } from '../types'
import { Brand, ErrorBox, Sheet } from '../components/ui'

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; dashboard: DashboardDTO }
  | { kind: 'error'; code: string }

export function HomeScreen({ auth, onNotConnected }: { auth: AuthContext; onNotConnected: () => void }) {
  const [load, setLoad] = useState<LoadState>({ kind: 'loading' })
  const [acting, setActing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pendingConfirmation, setPendingConfirmation] = useState<ConfirmationDTO | null>(null)

  const refresh = useCallback(async () => {
    try {
      const dashboard = await api.dashboard(auth)
      if (dashboard.copy_state === 'empty') {
        onNotConnected()
        return
      }
      setLoad({ kind: 'ready', dashboard })
    } catch (err) {
      setLoad({ kind: 'error', code: err instanceof ApiError ? err.code : 'network_error' })
    }
  }, [auth, onNotConnected])

  useEffect(() => {
    // Initial data fetch: refresh() only sets state after await (async), so
    // this is the standard external-system sync, not a cascading render.
    // oxlint-disable-next-line react/set-state-in-effect
    void refresh()
  }, [refresh])

  async function pause() {
    setActing(true)
    setError(null)
    try {
      const dashboard = await api.pause(auth)
      hapticImpact('medium')
      setLoad({ kind: 'ready', dashboard })
    } catch (err) {
      hapticNotify('error')
      setError(err instanceof ApiError ? err.code : 'network_error')
    } finally {
      setActing(false)
    }
  }

  async function resume(confirmation?: ConfirmationDTO) {
    setActing(true)
    setError(null)
    try {
      const dashboard = await api.resume(auth, confirmation)
      hapticNotify('success')
      setPendingConfirmation(null)
      setLoad({ kind: 'ready', dashboard })
    } catch (err) {
      if (err instanceof ApiError && err.code === 'confirmation_expired' && !confirmation) {
        // Master is in a position: the app requires an explicit confirmation.
        try {
          const pending = await api.beginConfirmation(auth, 'resume')
          setPendingConfirmation(pending)
        } catch (beginErr) {
          setError(beginErr instanceof ApiError ? beginErr.code : 'network_error')
        }
      } else {
        hapticNotify('error')
        setError(err instanceof ApiError ? err.code : 'network_error')
        setPendingConfirmation(null)
      }
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

      {error && <ErrorBox {...describeError(error)} />}

      <section className="panel state-card">
        <div className="state-icon" aria-hidden>
          {view.tone === 'active' ? '✓' : view.tone === 'paused' ? '⏸' : '!'}
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
          Система: {systemLabel(dashboard.system_state)} · Обновлено {formatTime(dashboard.updated_at)}
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

      <div className="info-box">
        Позиции видны в боте; полный кабинет позиций и истории появится в следующей версии.
      </div>

      {view.actionLabel === 'pause' && (
        <div className="btn-stack">
          <button type="button" className="btn btn-primary" disabled={acting} onClick={() => void pause()}>
            {acting ? 'Ставим на паузу…' : '⏸ Поставить на паузу'}
          </button>
          <p className="footer-note">Пауза останавливает только новые входы — защита продолжит работать.</p>
        </div>
      )}

      {view.actionLabel === 'resume' && (
        <div className="btn-stack">
          <button
            type="button"
            className="btn btn-primary"
            disabled={acting}
            onClick={() => void resume()}
          >
            {acting ? 'Возобновляем…' : '▶ Возобновить'}
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => void refresh()}
            disabled={acting}
          >
            Обновить данные
          </button>
        </div>
      )}

      {view.actionLabel === null && (
        <div className="btn-stack">
          <button type="button" className="btn btn-secondary" onClick={() => void refresh()}>
            Обновить данные
          </button>
        </div>
      )}

      {pendingConfirmation && (
        <Sheet title={RESUME_CONFIRM_TITLE} body={RESUME_CONFIRM_BODY}>
          <button
            type="button"
            className="btn btn-primary"
            disabled={acting}
            onClick={() => void resume(pendingConfirmation)}
          >
            Подтвердить возобновление
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
      return 'в порядке'
    case 'stale':
      return 'обновляется'
    case 'degraded':
      return 'деградация'
    case 'blocked':
      return 'заблокирована'
    default:
      return 'неизвестно'
  }
}
