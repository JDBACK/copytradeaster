/** Pure Home view-model (minimal part-1 scope over DashboardDTO). */

import type { DashboardDTO } from './types'
import { isBuilderVerified } from './accountBuilder'
import { formatTime } from './format'

export type HomeTone = 'active' | 'paused' | 'empty' | 'attention'

export interface HomeViewModel {
  title: string
  subtitle: string
  tone: HomeTone
  /** Primary action label; null when the action lives in the bot. */
  actionLabel: 'pause' | 'resume' | null
}

export function homeViewModel(dashboard: DashboardDTO): HomeViewModel {
  switch (dashboard.copy_state) {
    case 'active':
      return {
        title: 'Всё работает',
        subtitle: 'Копирование включено',
        tone: 'active',
        actionLabel: 'pause',
      }
    case 'paused':
      return {
        title: 'Новые входы на паузе',
        subtitle: 'Защита и закрытия продолжают работать',
        tone: 'paused',
        actionLabel: 'resume',
      }
    case 'empty':
      return {
        title: 'Аккаунт не подключён',
        subtitle: 'Подключите Aster, чтобы начать копирование',
        tone: 'empty',
        actionLabel: null,
      }
    case 'degraded':
      return {
        title: 'Требуется проверка',
        subtitle: 'Обнаружены расхождения — обновите статус',
        tone: 'attention',
        actionLabel: null,
      }
    case 'blocked':
      return {
        title: 'Копирование остановлено',
        subtitle: dashboard.message ?? 'Требуется действие — подробности в боте',
        tone: 'attention',
        actionLabel: null,
      }
    default:
      return {
        title: 'Состояние неизвестно',
        subtitle: 'Обновите данные или откройте бота',
        tone: 'attention',
        actionLabel: null,
      }
  }
}

export function snapshotLabel(capturedAt: string | null | undefined): string {
  return capturedAt ? formatTime(capturedAt) : 'не получен'
}

export interface TrustItem {
  key: string
  label: string
  done: boolean
}

/** Compact verified trust summary (эталон 02-home-active): Wallet → Agent →
 * Builder → Sync.  Every item is derived from backend facts; the Builder item
 * uses the single shared P2.6 attestation verdict (verified only). Sync
 * requires both system_state === 'ready' AND a fresh snapshot timestamp. */
export function trustSummary(dashboard: DashboardDTO): TrustItem[] {
  return [
    { key: 'wallet', label: 'Wallet', done: Boolean(dashboard.account_masked) },
    { key: 'agent', label: 'Agent', done: Boolean(dashboard.signer_masked) },
    { key: 'builder', label: 'Builder', done: isBuilderVerified(dashboard) },
    {
      key: 'sync',
      label: 'Sync',
      done: dashboard.system_state === 'ready' && Boolean(dashboard.snapshot_captured_at),
    },
  ]
}

/** Resume confirmation copy, without inaccurate safety claims. */
export const RESUME_CONFIRM_TITLE = 'Возобновить копирование?'
export const RESUME_CONFIRM_BODY =
  'Мастер сейчас в позиции. При возобновлении система может открыть позицию по текущей рыночной цене ' +
  'с расчётом риска и выставлением защиты.'
export const RESUME_CONFIRM_FLAT_BODY =
  'Новые входы снова станут доступны с текущими настройками риска. ' +
  'Закрытия и защитные действия продолжают работать.'
