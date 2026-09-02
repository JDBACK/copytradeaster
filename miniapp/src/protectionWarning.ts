/** Production warning copy per protection state — single source of truth for
 * the positions UI.  Extracted from the component file so view-model
 * behaviour is unit-testable without fast-refresh export warnings. */

import type { PositionSummaryDTO } from './types'

export type ProtectionState = PositionSummaryDTO['protection_state']

export const PROTECTION_WARNING: Partial<Record<ProtectionState, string>> = {
  missing_stop: 'Защита отсутствует (Stop не установлен)',
  partial_stop: 'Частичная защита (Stop покрывает не весь объем)',
  duplicate_stop: 'Дублированный Stop (сходимость в процессе)',
  unconfirmed: 'Защита требует проверки',
}
