import { useState } from 'react'
import type { AuthContext } from '../api'
import { Brand, ErrorBox } from '../components/ui'
import { describeError } from '../errors'
import { formatPnl, formatPrice, formatUsdt } from '../format'
import { PROTECTION_WARNING } from '../protectionWarning'
import type { PositionSummaryDTO } from '../types'
import type { NestedScreen } from '../navigation'
import { MiniAppResources } from '../resources'
import { useResource, useScreenResources, useTransportStale } from '../useResource'
import { PositionSummaryCard } from '../components/PositionSummaryCard'
import { orderedPositions } from '../tradeView'
import { ClosedTrades } from './TradeListScreen'

const WarningIcon = ({ size }: { size: number }) => <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden><path d="M12 9v4M12 17h.01" stroke="currentColor" strokeWidth="2" /><circle cx="12" cy="12" r="9" stroke="currentColor" /></svg>

export function PositionsScreen({ auth, accountMasked, active = true, warm = true, resources: providedResources, foregroundSignal, onOpen = () => {} }: {
  auth: AuthContext; accountMasked?: string | null; active?: boolean; warm?: boolean; resources?: MiniAppResources; foregroundSignal?: number; onOpen?: (screen: NestedScreen) => void
}) {
  const resources = useScreenResources(providedResources, auth)
  const [segment, setSegment] = useState<'open' | 'closed'>('open')
  const [attention, setAttention] = useState(false)
  const resource = useResource({ resources, key: 'positions', enabled: warm, active: active && segment === 'open', foregroundSignal })
  const data = resource.snapshot.data
  const transportUnhealthy = useTransportStale(resources, resource.snapshot.error)
  const stale = !!data && (data.is_stale || transportUnhealthy)
  const positions = orderedPositions(data?.positions ?? [], attention)
  return <div className="screen">
    <Brand right={accountMasked ? <span className="wallet-chip num">{accountMasked}</span> : undefined} />
    <h1 className="h1">Позиции</h1>
    <div className="history-segments" role="tablist" aria-label="Состояние позиций"><button type="button" role="tab" aria-selected={segment === 'open'} onClick={() => setSegment('open')}>Открытые {data?.count ?? '—'}</button><button type="button" role="tab" aria-selected={segment === 'closed'} onClick={() => setSegment('closed')}>Закрытые</button></div>
    <div hidden={segment !== 'closed'}><ClosedTrades resources={resources} onOpen={onOpen} active={active && segment === 'closed'} preview foregroundSignal={foregroundSignal} /></div>
    <div hidden={segment !== 'open'} className="history-stack">
      {resource.snapshot.error && <ErrorBox {...describeError(resource.snapshot.error)} />}
      {!data && !resource.snapshot.error && <div className="skeleton" style={{ minHeight: 240 }} aria-label="Загрузка позиций" />}
      {stale && <p className="position-warning" role="status">Снимок позиций не обновляется. Статус защиты может быть неполным.</p>}
      {data?.positions.some(p => p.protection_state !== 'protected') && <div className="history-segments"><button type="button" aria-pressed={!attention} onClick={() => setAttention(false)}>Все</button><button type="button" aria-pressed={attention} onClick={() => setAttention(true)}>Требуют внимания</button></div>}
      {positions.slice(0, 3).map(position => <PositionSummaryCard key={`${position.symbol}:${position.side}`} position={position} stale={stale} onOpen={() => onOpen(position.trade_ref ? { kind: 'trade-detail', tradeRef: position.trade_ref } : { kind: 'position-detail', symbol: position.symbol, side: position.side })} />)}
      {data && positions.length === 0 && <p className="info-box">{!data.snapshot_captured_at ? 'Получаем данные аккаунта' : attention ? 'Нет позиций с проблемой защиты' : 'Нет открытых позиций'}</p>}
      {positions.length > 3 && <button type="button" className="btn btn-secondary" onClick={() => onOpen({ kind: 'trade-list', state: 'open', period: 'all', attention })}>Показать все {positions.length}</button>}
    </div>
  </div>
}

export function PositionCard({ position }: { position: PositionSummaryDTO }) {
  const pnl = formatPnl(position.unrealized_profit)
  const isBtc = position.symbol.toUpperCase().startsWith('BTC')
  const isEth = position.symbol.toUpperCase().startsWith('ETH')
  const coinClass = isBtc ? 'btc' : isEth ? 'eth' : ''
  const coinInitial = isBtc ? '₿' : isEth ? 'Ξ' : position.symbol.slice(0, 1)

  return (
    <section className="position-card" aria-label={`Позиция ${position.symbol} ${position.side}`}>
      <div className="position-card-header">
        <div className="position-symbol-group">
          <div className={`coin-icon ${coinClass}`} aria-hidden>
            {coinInitial}
          </div>
          <div>
            <div className="position-symbol">{position.symbol}</div>
          </div>
          <span className={`side-badge ${position.side === 'LONG' ? 'long' : 'short'}`}>
            {position.side}
          </span>
        </div>
        <div className={`position-pnl num ${pnl.tone === 'positive' ? 'pnl-positive' : pnl.tone === 'negative' ? 'pnl-negative' : 'pnl-neutral'}`}>
          {pnl.text}
        </div>
      </div>

      {PROTECTION_WARNING[position.protection_state] && (
        <div className="position-warning" role="alert">
          <WarningIcon size={16} />
          <span>{PROTECTION_WARNING[position.protection_state]}</span>
        </div>
      )}

      <div className="position-table">
        <div className="position-row">
          <span className="position-row-label">Вход</span>
          <span className="position-row-value num">{formatPrice(position.entry_price)}</span>
        </div>
        <div className="position-row">
          <span className="position-row-label">Сейчас</span>
          <span className="position-row-value num">{formatPrice(position.mark_price)}</span>
        </div>
        <div className="position-row">
          <span className="position-row-label">Stop</span>
          <span className="position-row-value num">
            {position.stop_price ? formatPrice(position.stop_price) : '—'}
          </span>
        </div>
        {position.take_profit_price && (
          <div className="position-row">
            <span className="position-row-label">Take Profit</span>
            <span className="position-row-value num">{formatPrice(position.take_profit_price)}</span>
          </div>
        )}
        <div className="position-row">
          <span className="position-row-label">Риск до Stop</span>
          <span className="position-row-value num">
            {position.risk_to_stop_usdt !== null && position.risk_to_stop_usdt !== undefined
              ? formatUsdt(position.risk_to_stop_usdt)
              : '—'}
          </span>
        </div>
        <div className="position-row">
          <span className="position-row-label">Плечо</span>
          <span className="position-row-value num">{position.leverage}× авто</span>
        </div>
      </div>
    </section>
  )
}
