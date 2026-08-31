/** Pure Home view-model (minimal part-1 scope over DashboardDTO). */

import type { DashboardDTO } from './types'

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
        subtitle: 'Данные обновляются — действие пока не нужно',
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

/** Resume confirmation copy, mirrored from the bot UX (master in position). */
export const RESUME_CONFIRM_TITLE = 'Возобновить копирование?'
export const RESUME_CONFIRM_BODY =
  'Мастер сейчас в позиции. Новый вход повторит его сделку по текущей цене, ' +
  'риск и защита рассчитаются заново. Это безопасно, но вход будет не по цене мастера.'
