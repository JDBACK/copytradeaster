import { useState } from 'react'
import type { AuthContext } from '../api'
import type { MiniAppResources } from '../resources'
import type { NestedScreen } from '../navigation'
import type { TradePeriod } from '../types'
import { useResource } from '../useResource'
import { useHistory } from '../useHistory'
import { orderedPositions } from '../tradeView'
import { PositionSummaryCard } from '../components/PositionSummaryCard'
import { TradeSummaryCard } from '../components/TradeSummaryCard'
import { ErrorBox } from '../components/ui'
import { describeError } from '../errors'
import { formatDateTime } from '../format'

export function ClosedTrades({ resources, onOpen, active = true, preview = false, initialPeriod = 'all', foregroundSignal = 0 }: { resources: MiniAppResources; onOpen: (route: NestedScreen) => void; active?: boolean; preview?: boolean; initialPeriod?: TradePeriod; foregroundSignal?: number }) {
  const [period, setPeriod] = useState<TradePeriod>(initialPeriod)
  const [search, setSearch] = useState('')
  const [symbol, setSymbol] = useState('')
  const history = useHistory(resources.history, { kind: 'trades', period, symbol, limit: preview ? 3 : 20 }, active, foregroundSignal)
  return <>
    {!preview && <>
      <div className="history-segments" aria-label="Период истории">{(['7d', '30d', 'all'] as const).map(value => <button key={value} type="button" aria-pressed={period === value} onClick={() => setPeriod(value)}>{value === '7d' ? '7 дней' : value === '30d' ? '30 дней' : 'Вся доступная'}</button>)}</div>
      <form className="history-search" onSubmit={event => { event.preventDefault(); setSymbol(search.trim().toUpperCase()) }}><label>Инструмент<input value={search} onChange={event => setSearch(event.target.value)} placeholder="BTCUSDT" /></label><button type="submit" className="btn btn-secondary">Найти</button></form>
    </>}
    {history.data && <p className="freshness">{history.data.coverage_from ? `История доступна с ${formatDateTime(history.data.coverage_from)}` : 'Диапазон истории уточняется'} · {history.data.coverage_complete ? 'сверена' : 'может быть неполной'}</p>}
    {history.error && <><ErrorBox {...describeError(history.error)} /><button type="button" className="btn btn-secondary" onClick={() => void history.retry()}>{history.changed ? 'История изменилась — обновить' : 'Повторить загрузку'}</button></>}
    {!history.data && !history.error && <div className="skeleton" style={{ minHeight: 240 }} aria-label="Загрузка истории" />}
    {history.data?.items.map(trade => <TradeSummaryCard key={trade.trade_ref} trade={trade} onOpen={() => onOpen({ kind: 'trade-detail', tradeRef: trade.trade_ref })} />)}
    {history.data?.items.length === 0 && <p className="info-box">{history.data.coverage_from ? 'Закрытых сделок за этот период нет' : 'Загружаем историю. Доступный диапазон пока не подтверждён.'}</p>}
    {history.data?.next_cursor && <button type="button" className="btn btn-secondary" disabled={history.loading} onClick={() => preview ? onOpen({ kind: 'trade-list', state: 'closed', period }) : void history.more()}>{history.loading ? 'Загружаем…' : preview ? 'Вся история' : 'Показать ещё 20'}</button>}
    {history.data && <button type="button" className="btn btn-ghost" disabled={history.loading} onClick={() => void history.refresh()}>Обновить историю</button>}
  </>
}

export function TradeListScreen({ resources, route, onOpen, active = true, foregroundSignal = 0 }: { auth: AuthContext; resources: MiniAppResources; route: Extract<NestedScreen, { kind: 'trade-list' }>; onOpen: (route: NestedScreen) => void; active?: boolean; foregroundSignal?: number }) {
  const [search, setSearch] = useState('')
  const [attention, setAttention] = useState(route.attention ?? false)
  const positions = useResource({ resources, key: 'positions', active: active && route.state === 'open', enabled: route.state === 'open', foregroundSignal })
  const data = positions.snapshot.data
  return <div className="screen"><h1 className="h1">{route.state === 'closed' ? 'Закрытые сделки' : 'Открытые позиции'}</h1>
    {route.state === 'closed' ? <ClosedTrades resources={resources} onOpen={onOpen} active={active} initialPeriod={route.period} foregroundSignal={foregroundSignal} /> : <>
      <label className="history-search">Инструмент<input value={search} onChange={event => setSearch(event.target.value)} placeholder="Поиск по инструменту" /></label>
      <div className="history-segments"><button type="button" aria-pressed={!attention} onClick={() => setAttention(false)}>Все</button><button type="button" aria-pressed={attention} onClick={() => setAttention(true)}>Требуют внимания</button></div>
      {positions.snapshot.error && <ErrorBox {...describeError(positions.snapshot.error)} />}
      {!data && !positions.snapshot.error && <div className="skeleton" style={{ minHeight: 240 }} />}
      {data && orderedPositions(data.positions, attention, search).map(position => <PositionSummaryCard key={`${position.symbol}:${position.side}`} position={position} stale={data.is_stale || positions.snapshot.stale || !!positions.snapshot.error} onOpen={() => onOpen(position.trade_ref ? { kind: 'trade-detail', tradeRef: position.trade_ref } : { kind: 'position-detail', symbol: position.symbol, side: position.side })} />)}
      {data && !orderedPositions(data.positions, attention, search).length && <p className="info-box">{data.snapshot_captured_at ? 'По выбранному фильтру позиций нет' : 'Получаем данные позиций'}</p>}
      <button type="button" className="btn btn-secondary" disabled={positions.snapshot.inflight} onClick={() => void positions.refresh('strong').catch(() => {})}>Обновить позиции</button>
    </>}
  </div>
}
