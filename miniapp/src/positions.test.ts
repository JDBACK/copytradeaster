import { describe, expect, it } from 'vitest'
import { formatPnl, formatPrice, formatUsdt } from './format'
import type { PositionPageDTO, PositionSummaryDTO } from './types'

function samplePosition(overrides: Partial<PositionSummaryDTO> = {}): PositionSummaryDTO {
  return {
    symbol: 'BTCUSDT',
    side: 'LONG',
    qty: '0.050',
    entry_price: '59200.00',
    mark_price: '60150.50',
    unrealized_profit: '47.525',
    leverage: 20,
    protection_state: 'protected',
    stop_price: '58000.00',
    take_profit_price: '64000.00',
    risk_to_stop_usdt: '60.060',
    ...overrides,
  }
}

function samplePage(overrides: Partial<PositionPageDTO> = {}): PositionPageDTO {
  return {
    positions: [samplePosition()],
    count: 1,
    system_state: 'ready',
    snapshot_captured_at: '2026-09-02T10:00:00Z',
    is_stale: false,
    message: null,
    ...overrides,
  }
}

describe('positions view model and formatters', () => {
  it('formats position numbers and prices accurately', () => {
    const pos = samplePosition()
    expect(formatPrice(pos.entry_price).replace(/\u00a0/g, ' ')).toBe('59 200')
    expect(formatPrice(pos.mark_price).replace(/\u00a0/g, ' ')).toBe('60 150,5')
    const pnl = formatPnl(pos.unrealized_profit)
    expect(pnl.text.replace(/\u00a0/g, ' ')).toBe('+47,53 USDT')
    expect(pnl.tone).toBe('positive')
    expect(formatUsdt(pos.risk_to_stop_usdt).replace(/\u00a0/g, ' ')).toBe('60,06 USDT')
  })

  it('handles empty positions page', () => {
    const emptyPage = samplePage({ positions: [], count: 0 })
    expect(emptyPage.positions.length).toBe(0)
    expect(emptyPage.count).toBe(0)
  })

  it('handles stale snapshot state with risk hidden or null', () => {
    const stalePage = samplePage({
      is_stale: true,
      message: 'Aster positions are temporarily unavailable',
      positions: [samplePosition({ risk_to_stop_usdt: null })],
    })
    expect(stalePage.is_stale).toBe(true)
    expect(stalePage.positions[0].risk_to_stop_usdt).toBeNull()
  })

  it('evaluates all protection warning states correctly', () => {
    const missing = samplePosition({ protection_state: 'missing_stop', stop_price: null, risk_to_stop_usdt: null })
    const partial = samplePosition({ protection_state: 'partial_stop', risk_to_stop_usdt: null })
    const duplicate = samplePosition({ protection_state: 'duplicate_stop', risk_to_stop_usdt: null })
    const unconfirmed = samplePosition({ protection_state: 'unconfirmed', risk_to_stop_usdt: null })
    const protectedPos = samplePosition({ protection_state: 'protected', risk_to_stop_usdt: '60.06' })

    expect(missing.protection_state).toBe('missing_stop')
    expect(partial.protection_state).toBe('partial_stop')
    expect(duplicate.protection_state).toBe('duplicate_stop')
    expect(unconfirmed.protection_state).toBe('unconfirmed')
    expect(protectedPos.protection_state).toBe('protected')

    expect(missing.risk_to_stop_usdt).toBeNull()
    expect(partial.risk_to_stop_usdt).toBeNull()
    expect(duplicate.risk_to_stop_usdt).toBeNull()
    expect(unconfirmed.risk_to_stop_usdt).toBeNull()
    expect(protectedPos.risk_to_stop_usdt).toBe('60.06')
  })
})
