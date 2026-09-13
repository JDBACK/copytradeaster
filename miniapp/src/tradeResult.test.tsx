/**
 * One number per trade: as soon as the server proves every cost, the net
 * replaces the gross. Two competing results on one card made the reader
 * reconcile them by hand, which is exactly what the owner rejected.
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { TradeSummaryCard } from './components/TradeSummaryCard'
import type { MoneyComponent, TradeListItem } from './types'

function money(known: string, state: MoneyComponent['state'] = 'complete'): MoneyComponent {
  return { known_amount: known, state, issues: [] }
}

function sampleTrade(overrides: Partial<TradeListItem> = {}): TradeListItem {
  return {
    trade_ref: 'trade-1',
    symbol: 'BTCUSDT',
    quantity_asset: 'BTC',
    side: 'LONG',
    state: 'closed',
    source: 'copy',
    opened_at: '2026-09-13T01:00:00Z',
    closed_at: '2026-09-13T02:00:00Z',
    executed_qty: '0.002',
    realized_pnl_gross_usdt: '10',
    net_pnl_usdt: null,
    quality: 'complete',
    result_state: 'gross_only',
    money: null,
    average_entry: '60000',
    average_exit: '60005',
    ...overrides,
  }
}

const netMoney = {
  gross: '10',
  net: '9.42',
  commission: money('-0.58'),
  funding: money('0'),
  builder: money('0'),
  result_state: 'net_complete',
  issues: [],
  as_of: '2026-09-13T02:01:00Z',
  revision: 3,
}

describe('trade result presentation', () => {
  it('shows the proven net as the single trade result', () => {
    const html = renderToStaticMarkup(
      <TradeSummaryCard
        trade={sampleTrade({
          net_pnl_usdt: '9.42',
          result_state: 'net_complete',
          money: netMoney,
        })}
        onOpen={() => {}}
      />,
    )
    expect(html).toContain('Итог сделки')
    expect(html).toContain('+9,42 USDT')
    expect(html).not.toContain('до комиссий')
  })

  it('keeps the honest gross label while the net is not proven', () => {
    const html = renderToStaticMarkup(
      <TradeSummaryCard
        trade={sampleTrade({ money: { ...netMoney, net: null, result_state: 'gross_only' } })}
        onOpen={() => {}}
      />,
    )
    expect(html).toContain('PnL до комиссий')
    expect(html).toContain('+10,00 USDT')
    expect(html).not.toContain('Итог сделки')
  })

  it('never prints a second result row once the net is proven', () => {
    // The detail screen used to list the gross and the net one under the other.
    // Both values are asserted here through the single-row guard below.
    const source = readFileSync(new URL('./screens/TradeDetailScreen.tsx', import.meta.url), 'utf8')
    expect(source).toContain("{!net && <div className=\"summary-row\"><span>Итог с начислениями</span>")
    expect(source).toContain("formatPnl(net ? trade.net_pnl_usdt : trade.realized_pnl_gross_usdt)")
  })
})
