import type { TradeListItem } from '../types'
import { formatDateTime, formatDuration, formatPnl, formatPrice } from '../format'
export function TradeSummaryCard({ trade, onOpen }: { trade: TradeListItem; onOpen: () => void }) {
  const complete = trade.result_state === 'net_complete' && trade.net_pnl_usdt !== null
  const pnl = formatPnl(complete ? trade.net_pnl_usdt : trade.realized_pnl_gross_usdt)
  return <button type="button" className="position-card summary-card" onClick={onOpen}>
    <span className="trade-card-top"><strong>{trade.symbol} · {trade.side ?? 'Направление уточняется'}</strong><strong className={`num pnl-${pnl.tone}`}>{pnl.text}</strong></span>
    <span className="trade-card-top tone-muted"><span>Закрыта {formatDateTime(trade.closed_at)}</span><span>{complete ? 'Итог сделки' : 'PnL до комиссий'}</span></span>
    <span className="trade-card-top num"><span>Вход {formatPrice(trade.average_entry)}</span><span>Выход {formatPrice(trade.average_exit)}</span></span>
    <span className="tone-muted">В позиции {formatDuration(trade.opened_at, trade.closed_at)}{trade.quality !== 'complete' ? ' · История неполная' : ''}</span>
  </button>
}
