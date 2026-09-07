import type { PositionSummaryDTO } from './types'
export function orderedPositions(positions: PositionSummaryDTO[], attention = false, search = '') {
  return positions.filter(p => (!attention || p.protection_state !== 'protected') && p.symbol.toUpperCase().includes(search.trim().toUpperCase())).sort((a, b) => Number(a.protection_state === 'protected') - Number(b.protection_state === 'protected') || a.symbol.localeCompare(b.symbol) || a.side.localeCompare(b.side))
}
export const MONEY_ISSUES: Record<string, string> = {
  partial_history: 'Начало истории восстановлено не полностью', income_incomplete: 'Начисления ещё не сверены', fill_missing: 'Не все исполнения доступны', shared_fill_commission: 'Комиссия общего исполнения уточняется', commission_missing: 'Комиссия не подтверждена', commission_mismatch: 'Комиссия требует сверки', non_usdt_cost: 'Есть начисление в другой валюте', funding_unattributed: 'Funding не распределён', builder_unknown: 'Комиссия Builder не подтверждена', builder_unattributed: 'Комиссия Builder не распределена', builder_policy_conflict: 'Требуется сверка комиссии Builder',
}
export const moneyIssue = (issue: string) => MONEY_ISSUES[issue] ?? 'Состав результата требует сверки'
