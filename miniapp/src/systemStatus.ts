/** Pure System Status view-model (Stage 3 part 2, P2.5).
 *
 * Maps the authoritative backend StatusDTO into row values shown on the
 * System Status screen.  Nothing here invents facts: health comes only from
 * ``dto.state``, the Builder rail step stays neutral until P2.6, and an
 * unknown runtime remediation value is never executable (maps to null).
 */

import { isBuilderVerified } from './accountBuilder'
import { formatTime } from './format'
import type {
  AsterState,
  CopyState,
  PermissionsState,
  StatusDTO,
  StatusReason,
  StatusRemediation,
  StatusState,
  SystemState,
} from './types'

export type StatusTone = 'success' | 'warning' | 'danger' | 'muted'

export interface StatusValue {
  label: string
  tone: StatusTone
}

/** Overall health block: only backend ``state=ready`` reads healthy. */
export function overallValue(state: StatusState | undefined): StatusValue {
  switch (state) {
    case 'ready':
      return { label: 'Всё работает', tone: 'success' }
    case 'stale':
      return { label: 'Данные устарели', tone: 'warning' }
    case 'degraded':
      return { label: 'Требуется проверка', tone: 'warning' }
    case 'blocked':
      return { label: 'Требуется действие', tone: 'danger' }
    default:
      return { label: 'Неизвестно', tone: 'muted' }
  }
}

export function systemRowValue(state: SystemState | undefined): StatusValue {
  switch (state) {
    case 'ready':
      return { label: 'Работает', tone: 'success' }
    case 'stale':
      return { label: 'Данные устарели', tone: 'warning' }
    case 'degraded':
      return { label: 'Деградация', tone: 'danger' }
    default:
      return { label: 'Неизвестно', tone: 'muted' }
  }
}

export function copyRowValue(state: CopyState | undefined): StatusValue {
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

export function asterRowValue(state: AsterState | undefined): StatusValue {
  switch (state) {
    case 'ok':
      return { label: 'Подключён', tone: 'success' }
    case 'stale':
      return { label: 'Данные устарели', tone: 'warning' }
    case 'blocked':
      return { label: 'Недоступен', tone: 'danger' }
    default:
      return { label: 'Неизвестно', tone: 'muted' }
  }
}

export function permissionsRowValue(state: PermissionsState | undefined): StatusValue {
  switch (state) {
    case 'valid':
      return { label: 'Подтверждены', tone: 'success' }
    case 'stale':
      return { label: 'Устарели', tone: 'warning' }
    case 'blocked':
      return { label: 'Отклонены', tone: 'danger' }
    default:
      return { label: 'Не проверены', tone: 'muted' }
  }
}

export function positionsRowValue(count: number | null | undefined): StatusValue {
  if (typeof count !== 'number' || !Number.isFinite(count)) {
    return { label: '—', tone: 'muted' }
  }
  return { label: String(count), tone: 'muted' }
}

/** Honest age copy from the backend-computed seconds; never a health verdict. */
export function formatAge(ageSeconds: number | null | undefined): string {
  if (typeof ageSeconds !== 'number' || !Number.isFinite(ageSeconds) || ageSeconds < 0) {
    return '—'
  }
  if (ageSeconds < 5) return 'только что'
  if (ageSeconds < 60) return `${Math.floor(ageSeconds)} сек назад`
  const minutes = Math.floor(ageSeconds / 60)
  if (minutes < 60) return `${minutes} мин назад`
  const hours = Math.floor(minutes / 60)
  return `${hours} ч назад`
}

export function lastCheckRowValue(
  lastCheckAt: string | null | undefined,
  ageSeconds: number | null | undefined,
): StatusValue {
  if (!lastCheckAt) return { label: 'Не проверялось', tone: 'muted' }
  return { label: `${formatTime(lastCheckAt)} · ${formatAge(ageSeconds)}`, tone: 'muted' }
}

/** Typed reason copy; null means no banner (fresh ``none``). */
export function reasonCopy(reason: StatusReason | undefined): string | null {
  switch (reason) {
    case 'none':
    case undefined:
      return null
    case 'account_not_connected':
      return 'Счёт подписчика не подключён.'
    case 'account_disconnected':
      return 'Счёт отключён. Подключите его заново, чтобы продолжить.'
    case 'system_not_ready':
      return 'Система пока не готова.'
    case 'system_stale':
      return 'Данные системы устарели.'
    case 'system_degraded':
      return 'Система требует проверки.'
    case 'aster_unavailable':
      return 'Aster временно не отвечает. Показаны последние сохранённые данные.'
    case 'snapshot_missing':
      return 'Снимок позиций пока не получен.'
    case 'snapshot_stale':
      return 'Снимок позиций устарел.'
    case 'permission_unavailable':
      return 'Права подключения пока не проверены.'
    case 'permission_stale':
      return 'Проверка прав устарела.'
    case 'permission_invalid':
      return 'Права подключения отклонены. Продолжите в чате с ботом.'
    case 'copy_blocked':
      return 'Копирование заблокировано. Продолжите в чате с ботом.'
    case 'copy_degraded':
      return 'Копирование требует проверки.'
    default:
      return null
  }
}

/** Closed remediation mapping: unknown runtime values never execute. */
export function remediationOf(
  remediation: StatusRemediation | string | null | undefined,
): StatusRemediation | null {
  if (remediation === 'refresh' || remediation === 'open_bot') return remediation
  return null
}

export interface StatusTrustItem {
  key: string
  label: string
  done: boolean
}

/** Trust rail over StatusDTO evidence (same Wallet → Agent → Builder → Sync
 * direction as Home).  Builder uses the single shared P2.6 attestation
 * verdict (verified only); Sync requires the authoritative overall
 * ``ready`` verdict. */
export function statusTrust(dto: StatusDTO): StatusTrustItem[] {
  return [
    { key: 'wallet', label: 'Wallet', done: Boolean(dto.account_masked) },
    {
      key: 'agent',
      label: 'Agent',
      done: Boolean(dto.signer_masked) && dto.permissions_state === 'valid',
    },
    { key: 'builder', label: 'Builder', done: isBuilderVerified(dto) },
    { key: 'sync', label: 'Sync', done: dto.state === 'ready' },
  ]
}

/** Marker shown when the transport failed but a previous DTO stays on screen. */
export const TRANSPORT_STALE_WARNING = 'Связь с сервером потеряна. Показаны последние полученные данные.'
