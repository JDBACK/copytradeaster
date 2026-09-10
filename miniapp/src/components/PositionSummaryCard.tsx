import type { PositionSummaryDTO } from '../types'
import { formatPnl, formatPrice, formatQuantity } from '../format'
import { PROTECTION_WARNING } from '../protectionWarning'

/** One position card. Staleness is never repeated here: the screen's single
 * warning (or the shell rail) already says the picture may be out of date, and
 * twenty cards each claiming "статус стопа не обновлён" is noise, not safety. */
export function PositionSummaryCard({ position, onOpen }: { position: PositionSummaryDTO; onOpen?: () => void }) {
  const pnl = formatPnl(position.unrealized_profit)
  return <button type="button" className="position-card summary-card" onClick={onOpen} aria-label={`Позиция ${position.symbol} ${position.side}`}>
    <span className="trade-card-top"><strong>{position.symbol} · {position.side}</strong><strong className={`num pnl-${pnl.tone}`}>{pnl.text}</strong></span>
    <span className="trade-card-top tone-muted"><span className="num">{formatQuantity(position.qty, position.quantity_asset)} · {position.leverage}×</span><span>PnL сейчас</span></span>
    <span className="trade-card-top num"><span>Вход {formatPrice(position.entry_price)}</span><span>Марк. {formatPrice(position.mark_price)}</span></span>
    <span className={position.protection_state !== 'protected' ? 'tone-warning' : 'tone-muted'}>{position.protection_state === 'protected' ? `Стоп ${formatPrice(position.stop_price)} · подтверждён` : PROTECTION_WARNING[position.protection_state]}</span>
  </button>
}
