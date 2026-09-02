import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { formatPnl, formatPrice, formatUsdt } from './format'
import { PROTECTION_WARNING } from './protectionWarning'
import { PositionCard } from './screens/PositionsScreen'
import type { PositionSummaryDTO } from './types'

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
})

describe('PositionCard render (read-only UI contract)', () => {
  it('renders protected position with risk, stop, tp and leverage', () => {
    const html = renderToStaticMarkup(<PositionCard position={samplePosition()} />)
    expect(html).toContain('BTCUSDT')
    expect(html).toContain('LONG')
    expect(html).toContain('Вход')
    expect(html).toContain('Сейчас')
    expect(html).toContain('Stop')
    expect(html).toContain('Take Profit')
    expect(html).toContain('Риск до Stop')
    expect(html).toContain('Плечо')
    expect(html).toContain('20× авто')
  })

  it('contains no trading controls (strictly read-only)', () => {
    for (const state of [
      'protected',
      'missing_stop',
      'partial_stop',
      'duplicate_stop',
      'unconfirmed',
    ] as const) {
      const html = renderToStaticMarkup(
        <PositionCard
          position={samplePosition({ protection_state: state, risk_to_stop_usdt: null })}
        />,
      )
      const lower = html.toLowerCase()
      for (const forbidden of [
        '<button',
        'close',
        'reverse',
        'buy',
        'sell',
        'закрыть',
        'перевернуть',
        'купить',
        'продать',
      ]) {
        expect(lower).not.toContain(forbidden)
      }
    }
  })

  it.each([
    ['missing_stop', 'Защита отсутствует (Stop не установлен)'],
    ['partial_stop', 'Частичная защита (Stop покрывает не весь объем)'],
    ['duplicate_stop', 'Дублированный Stop (сходимость в процессе)'],
    ['unconfirmed', 'Защита требует проверки'],
  ])('renders warning copy and alert role for %s', (state, copy) => {
    const html = renderToStaticMarkup(
      <PositionCard
        position={samplePosition({ protection_state: state as PositionSummaryDTO['protection_state'] })}
      />,
    )
    expect(html).toContain('role="alert"')
    expect(html).toContain(copy)
  })

  it('renders no warning block for a protected position', () => {
    const html = renderToStaticMarkup(<PositionCard position={samplePosition()} />)
    expect(html).not.toContain('role="alert"')
  })

  it('hides risk value when risk_to_stop_usdt is null (stale/unconfirmed)', () => {
    const html = renderToStaticMarkup(
      <PositionCard
        position={samplePosition({ risk_to_stop_usdt: null, protection_state: 'missing_stop' })}
      />,
    )
    expect(html).toContain('—')
    expect(html).not.toContain('60,06')
  })

  it('exposes PROTECTION_WARNING mapping used by the card', () => {
    expect(Object.keys(PROTECTION_WARNING).sort()).toEqual([
      'duplicate_stop',
      'missing_stop',
      'partial_stop',
      'unconfirmed',
    ])
    expect(PROTECTION_WARNING.protected).toBeUndefined()
  })
})
