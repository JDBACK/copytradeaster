/** Notifications screen (nested, эталон 06-settings, P2.3).
 *
 * One toggle (role="switch") backed by the shared application use case.
 * The UI flips only on the server-confirmed DTO; a failed save keeps the
 * last confirmed state; double taps cannot start a second mutation.
 * Delivery facts (last delivery, pending, failed counts) come from the
 * same safe DTO — no outbox internals ever reach this screen. */

import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { api, ApiError, type AuthContext } from '../api'
import {
  createToggleGate,
  failedCountTone,
  initialNotificationsState,
  reduceNotifications,
  type ToggleGate,
} from '../notificationsView'
import { hapticImpact, hapticNotify } from '../telegram'
import { Brand, ErrorBox, Row } from '../components/ui'
import { describeError } from '../errors'
import { formatTime } from '../format'

export function NotificationsScreen({
  auth,
  accountMasked,
}: {
  auth: AuthContext
  accountMasked?: string | null
}) {
  const [state, dispatch] = useReducer(reduceNotifications, undefined, initialNotificationsState)
  const [initError, setInitError] = useState<string | null>(null)
  // Synchronous double-tap lock, created once per screen lifetime: state.saving
  // only exists after a re-render, so two taps in one tick must be gated here.
  const gate = useRef<ToggleGate | null>(null)
  if (gate.current === null) gate.current = createToggleGate()

  const loadInitial = useCallback(async () => {
    setInitError(null)
    try {
      const dto = await api.notifications(auth)
      dispatch({ type: 'save_succeeded', dto })
    } catch (err) {
      setInitError(err instanceof ApiError ? err.code : 'network_error')
    }
  }, [auth])

  useEffect(() => {
    // Initial fetch only: loadInitial awaits before its first setState.
    // oxlint-disable-next-line react/set-state-in-effect
    void loadInitial()
  }, [loadInitial])

  const save = useCallback(
    async (next: boolean) => {
      try {
        const dto = await api.setNotifications(auth, next)
        dispatch({ type: 'save_succeeded', dto })
        hapticNotify('success')
      } catch (err) {
        dispatch({
          type: 'save_failed',
          code: err instanceof ApiError ? err.code : 'network_error',
        })
        hapticNotify('error')
      } finally {
        // The mutation settled: reopen the synchronous lock.
        gate.current?.end()
      }
    },
    [auth],
  )

  const handleToggle = useCallback(() => {
    if (state.confirmed === null) return
    // Gate first: two taps in the same tick (before React re-renders saving)
    // must admit exactly one mutation; state.saving is only a render backstop.
    if (!gate.current?.tryBegin()) return
    dispatch({ type: 'save_requested' })
    void save(!state.confirmed.enabled)
  }, [state, save])

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

  if (state.confirmed === null) {
    if (initError) {
      const copy = describeError(initError)
      return (
        <div className="screen">
          {brand}
          <h1 className="h1" style={{ marginTop: 4 }}>
            Уведомления
          </h1>
          <ErrorBox title={copy.title} hint={copy.hint} />
          <div className="btn-stack">
            <button type="button" className="btn btn-secondary" onClick={() => void loadInitial()}>
              Повторить
            </button>
          </div>
        </div>
      )
    }
    return (
      <div className="screen">
        {brand}
        <h1 className="h1" style={{ marginTop: 4 }}>
          Уведомления
        </h1>
        <div
          className="skeleton"
          style={{ minHeight: 96 }}
          aria-label="Загрузка настроек уведомлений"
        />
      </div>
    )
  }

  const dto = state.confirmed

  return (
    <div className="screen">
      {brand}
      <h1 className="h1" style={{ marginTop: 4 }}>
        Уведомления
      </h1>
      <div className="lede tone-muted">События копирования в чат с ботом</div>

      {state.error && (
        <ErrorBox
          title={describeError(state.error).title}
          hint="Настройка не изменилась — показано последнее подтверждённое состояние."
        />
      )}

      <section className="panel notifications-toggle-panel">
        <div className="notifications-toggle-body">
          <div className="notifications-toggle-title">Уведомления о сделках</div>
          <div className="notifications-toggle-hint tone-muted">
            {dto.enabled
              ? 'Доставка включена: открытия, защиты и закрытия приходят в Telegram.'
              : 'Доставка выключена. Копирование не меняется — сделки продолжают исполняться.'}
          </div>
        </div>
        <NotificationToggle enabled={dto.enabled} saving={state.saving} onToggle={handleToggle} />
      </section>

      <div className="panel">
        <Row
          label="Последняя доставка"
          value={dto.last_delivery_at ? formatTime(dto.last_delivery_at) : 'ещё не было'}
        />
        <Row label="В очереди" value={String(dto.pending)} />
        <Row
          label="Не доставлено"
          value={String(dto.failed)}
          valueClass={failedCountTone(dto.failed) === 'danger' ? 'tone-danger' : ''}
        />
      </div>

      <div className="info-box notifications-safe-note">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
          <path
            d="M12 3.5 19 6v5.2c0 4.6-2.9 7.6-7 9.3-4.1-1.7-7-4.7-7-9.3V6l7-2.5Z"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinejoin="round"
          />
        </svg>
        <span>Отключение уведомлений не влияет на торговлю</span>
      </div>

      <span data-notifications-saving={String(state.saving)} hidden />
    </div>
  )
}

/** Accessible switch: button with role="switch"; disabled while saving so a
 * double tap cannot start a second mutation. */
export function NotificationToggle({
  enabled,
  saving,
  onToggle,
}: {
  enabled: boolean
  saving: boolean
  onToggle: () => void
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      aria-label="Уведомления о сделках"
      aria-busy={saving}
      className={`switch${enabled ? ' on' : ''}`}
      disabled={saving}
      onClick={() => {
        hapticImpact('light')
        onToggle()
      }}
    >
      <span className="switch-knob" aria-hidden />
    </button>
  )
}
