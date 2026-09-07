/** Notifications screen (nested, эталон 06-settings, P2.3).
 *
 * One toggle (role="switch") backed by the shared application use case.
 * The UI flips only on the server-confirmed DTO; a failed save keeps the
 * last confirmed state; double taps cannot start a second mutation.
 * Delivery facts (last delivery, pending, failed counts) come from the
 * same safe DTO — no outbox internals ever reach this screen. */

import { useCallback, useEffect, useState } from 'react'
import { api, ApiError, type AuthContext } from '../api'
import {
  failedCountTone,
} from '../notificationsView'
import { hapticImpact, hapticNotify } from '../telegram'
import { Brand, ErrorBox, Row } from '../components/ui'
import { describeError } from '../errors'
import { formatTime } from '../format'
import { MiniAppResources } from '../resources'
import { useResource, useScreenResources } from '../useResource'

export function NotificationsScreen({
  auth,
  accountMasked,
  onSaved,
  resources: providedResources,
  foregroundSignal,
}: {
  auth: AuthContext
  accountMasked?: string | null
  /** Fired after a successful save so the shell can stale dependent tabs. */
  onSaved?: () => void
  resources?: MiniAppResources
  foregroundSignal?: number
}) {
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const resources = useScreenResources(providedResources, auth)
  const ownerMutationState = resources.mutationState('notifications')
  const ownerUnknownSince = resources.mutationUnknownSinceRevision('notifications')
  const resource = useResource({
    resources,
    key: 'notifications',
    active: true,
    strength: ownerMutationState === 'unknown' ? 'strong' : 'weak',
    foregroundSignal,
  })
  const dto = resource.snapshot.data
  const ownerSaving = ownerMutationState !== null
  const transportStale =
    dto !== null && (resource.snapshot.stale || resource.snapshot.error !== null)

  useEffect(() => {
    if (ownerMutationState !== 'unknown' || ownerUnknownSince === null) return
    const snapshot = resource.snapshot
    if (
      snapshot.revision > ownerUnknownSince &&
      snapshot.data !== null &&
      snapshot.error === null &&
      !snapshot.stale &&
      snapshot.strength === 'strong'
    ) {
      resources.resolveUnknownMutation('notifications')
      resources.invalidate(['dashboard'])
    }
  }, [ownerMutationState, ownerUnknownSince, resource.snapshot, resources])

  const save = useCallback(
    async (next: boolean) => {
      const release = resources.acquireMutation('notifications')
      if (!release) return
      setSaving(true)
      setSaveError(null)
      try {
        const confirmed = await api.setNotifications(auth, next)
        resources.publishConfirmed('notifications', confirmed)
        resources.invalidate(['dashboard'])
        hapticNotify('success')
        onSaved?.()
      } catch (err) {
        if (resources.handleInvalidSessionError(err)) return
        const code = err instanceof ApiError ? err.code : 'network_error'
        let verified = false
        if (code === 'network_error' || code === 'timeout_error') {
          resources.invalidate(['notifications'])
          try {
            const current = await resources.refreshCurrent('notifications', 'strong')
            if (current.enabled === next) {
              resources.publishConfirmed('notifications', current)
              resources.invalidate(['dashboard'])
              onSaved?.()
              hapticNotify('success')
              verified = true
            }
          } catch {
            // Double-unknown is retained by the session owner across remount;
            // later authoritative evidence may resolve it, but the write is
            // never replayed automatically.
            resources.markMutationUnknown('notifications', 'notifications')
          }
        }
        if (!verified) {
          setSaveError(code)
          hapticNotify('error')
        }
      } finally {
        setSaving(false)
        release()
      }
    },
    [auth, onSaved, resources],
  )

  const handleToggle = useCallback(() => {
    if (!dto || ownerSaving) return
    void save(!dto.enabled)
  }, [dto, ownerSaving, save])

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

  if (dto === null) {
    if (resource.snapshot.error) {
      const copy = describeError(resource.snapshot.error)
      return (
        <div className="screen">
          {brand}
          <h1 className="h1" style={{ marginTop: 4 }}>
            Уведомления
          </h1>
          <ErrorBox title={copy.title} hint={copy.hint} />
          <div className="btn-stack">
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => void resource.refresh('weak').catch(() => {})}
            >
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

  return (
    <div className="screen">
      {brand}
      <h1 className="h1" style={{ marginTop: 4 }}>
        Уведомления
      </h1>
      <div className="lede tone-muted">События копирования в чат с ботом</div>

      {saveError && (
        <ErrorBox
          title={describeError(saveError).title}
          hint="Настройка не изменилась — показано последнее подтверждённое состояние."
        />
      )}

      {transportStale && (
        <div className="info-box notifications-safe-note" role="status">
          Показано последнее подтверждённое состояние. Данные требуют проверки перед изменением.
        </div>
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
        <NotificationToggle
          enabled={dto.enabled}
          saving={saving || ownerSaving || transportStale}
          onToggle={handleToggle}
        />
      </section>

      {(ownerMutationState === 'unknown' || resource.snapshot.error !== null) && (
        <div className="btn-stack">
          <button
            type="button"
            className="btn btn-secondary"
            disabled={resource.snapshot.inflight}
            onClick={() =>
              void resource
                .refresh(ownerMutationState === 'unknown' ? 'strong' : 'weak')
                .catch(() => {})
            }
          >
            Обновить данные
          </button>
        </div>
      )}

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

      <span data-notifications-saving={String(saving || ownerSaving)} hidden />
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
