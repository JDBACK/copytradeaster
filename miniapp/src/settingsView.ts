/** Pure Settings view-model (эталон 06-settings, P2.3).
 *
 * Maps authoritative backend states into the row values shown on the
 * Settings root.  Nothing here invents facts: stale/degraded states are
 * never presented as "Всё работает", and the Builder row stays neutral
 * until a persisted attestation exists (P2.6). */

import type { CopyState, SystemState } from './types'

export type SettingsTone = 'success' | 'warning' | 'danger' | 'muted'

export interface SettingValue {
  label: string
  tone: SettingsTone
}

/** Top summary panel: copying state of the connected follower account. */
export function copySummary(state: CopyState): SettingValue {
  switch (state) {
    case 'active':
      return { label: 'Включено', tone: 'success' }
    case 'paused':
      return { label: 'На паузе', tone: 'warning' }
    case 'degraded':
      return { label: 'Требует проверки', tone: 'warning' }
    case 'blocked':
      return { label: 'Остановлено', tone: 'danger' }
    case 'empty':
      return { label: 'Не подключено', tone: 'muted' }
    default:
      return { label: 'Неизвестно', tone: 'muted' }
  }
}

/** «Статус Aster» row value: «Всё работает» requires a ready system AND a
 * captured snapshot.  Ready without a snapshot has not proven end-to-end
 * freshness yet and reads «Ожидание данных» instead. */
export function systemStatusValue(
  state: SystemState,
  snapshotCapturedAt?: string | null,
): SettingValue {
  switch (state) {
    case 'ready':
      return snapshotCapturedAt
        ? { label: 'Всё работает', tone: 'success' }
        : { label: 'Ожидание данных', tone: 'muted' }
    case 'stale':
      return { label: 'Данные устарели', tone: 'warning' }
    case 'degraded':
      return { label: 'Требует проверки', tone: 'warning' }
    case 'blocked':
      return { label: 'Требует действия', tone: 'danger' }
    default:
      return { label: 'Неизвестно', tone: 'muted' }
  }
}

/** «Уведомления» row value from the authoritative dashboard flag. */
export function notificationsValue(enabled: boolean): SettingValue {
  return enabled
    ? { label: 'Включены', tone: 'success' }
    : { label: 'Отключены', tone: 'muted' }
}
