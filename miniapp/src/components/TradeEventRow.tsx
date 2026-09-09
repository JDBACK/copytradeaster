import type { TradeEventItem } from '../types'
import { formatTime } from '../format'
export function TradeEventRow({ event, onOpen }: { event: TradeEventItem; onOpen?: (ref: string) => void }) {
  const [title, ...lines] = event.text.split('\n')
  return <article className="trade-event"><time className="num tone-muted" dateTime={event.occurred_at}>{formatTime(event.occurred_at)}</time><div><strong>{title}</strong>{lines.map((line, index) => <p className="tone-muted" key={index}>{line}</p>)}{event.trade_ref && onOpen && <button type="button" className="btn btn-ghost" onClick={() => onOpen(event.trade_ref!)}>Открыть сделку</button>}</div></article>
}
