import type { MiniAppResources } from '../resources'
import type { NestedScreen } from '../navigation'
import { useHistory } from '../useHistory'
import { useResource, useTransportStale } from '../useResource'
import { ErrorBox } from '../components/ui'
import { describeError } from '../errors'
import { formatDateTime, formatDuration, formatPnl, formatPrice, formatQuantity, formatUsdt } from '../format'
import { groupTradeFills, moneyIssue } from '../tradeView'
import { TradeEventRow } from '../components/TradeEventRow'
import { PositionCard } from './PositionsScreen'

export function PositionDetailScreen({ resources, symbol, side, active = true, foregroundSignal = 0 }: { resources: MiniAppResources; symbol: string; side: 'LONG' | 'SHORT'; active?: boolean; foregroundSignal?: number }) {
  const snapshot = useResource({ resources, key: 'positions', active, foregroundSignal })
  const transportStale = useTransportStale(resources, snapshot.snapshot.error)
  const position = snapshot.snapshot.data?.positions.find(p => p.symbol === symbol && p.side === side)
  return <div className="screen"><h1 className="h1">{symbol} · {side}</h1>
    {snapshot.snapshot.error && <ErrorBox {...describeError(snapshot.snapshot.error)} />}
    {position ? <><p className="lede num">{formatQuantity(position.qty, position.quantity_asset)}</p><PositionCard position={position} /><p className="info-box">{snapshot.snapshot.data?.is_stale || transportStale ? 'Снимок устарел. Защита требует проверки. ' : ''}История этой позиции ещё уточняется. Отображается текущий снимок аккаунта.</p></> : <p className="info-box">{snapshot.snapshot.data ? 'Позиция больше не отображается в текущем снимке. Проверьте закрытые сделки.' : 'Получаем данные позиции'}</p>}
    <p className="freshness">Снимок: {formatDateTime(snapshot.snapshot.data?.snapshot_captured_at)}</p>
  </div>
}

export function TradeDetailScreen({ resources, tradeRef, onOpen, active = true, foregroundSignal = 0 }: { resources: MiniAppResources; tradeRef: string; onOpen: (route: NestedScreen) => void; active?: boolean; foregroundSignal?: number }) {
  const detail = useHistory(resources.history, { kind: 'detail', tradeRef }, active, foregroundSignal)
  const data = detail.data && 'trade' in detail.data ? detail.data : null
  const resolution = detail.data && 'kind' in detail.data ? detail.data : null
  const events = useHistory(resources.history, { kind: 'events', tradeRef }, active && !!data, foregroundSignal)
  const positions = useResource({ resources, key: 'positions', enabled: data?.trade.state === 'open', active: active && data?.trade.state === 'open', foregroundSignal })
  const transportStale = useTransportStale(resources, positions.snapshot.error)
  const trade = data?.trade
  if (resolution) return <div className="screen"><h1 className="h1">История сделки уточнена</h1><p className="info-box">Исходная запись разделена на несколько эпизодов. Выберите эпизод для просмотра.</p>{resolution.targets.map((ref, index) => <button type="button" className="btn btn-secondary" key={ref} onClick={() => onOpen({ kind: 'trade-detail', tradeRef: ref })}>Открыть эпизод {index + 1}</button>)}<button type="button" className="btn btn-ghost" onClick={() => onOpen({ kind: 'trade-list', state: 'closed', period: 'all' })}>Открыть историю</button></div>
  if (!trade || !data) return <div className="screen"><h1 className="h1">Детали сделки</h1>{detail.error ? <><ErrorBox {...describeError(detail.error)} /><button type="button" className="btn btn-ghost" onClick={() => onOpen({ kind: 'trade-list', state: 'open', period: 'all' })}>Мои позиции</button></> : <div className="skeleton" style={{ minHeight: 300 }} />}</div>
  const live = positions.snapshot.data?.positions.find(p => p.trade_ref === trade.trade_ref)
  const liveStale = !!positions.snapshot.data && (positions.snapshot.data.is_stale || transportStale)
  const net = trade.result_state === 'net_complete' && trade.net_pnl_usdt !== null
  const pnl = formatPnl(trade.state === 'open' ? live?.unrealized_profit : net ? trade.net_pnl_usdt : trade.realized_pnl_gross_usdt)
  return <div className="screen">
    <h1 className="h1">{trade.symbol} · {trade.side ?? 'Направление уточняется'}</h1>
    <p className="lede">{trade.state === 'open' ? 'Открыта' : 'Закрыта'} · {{ copy: 'Копирование', external: 'Внешние операции', mixed: 'Смешанные операции', unknown: 'Источник уточняется' }[trade.source]}</p>
    {detail.error && <ErrorBox {...describeError(detail.error)} />}
    {detail.error && <p className="freshness">Показаны ранее полученные данные. Сверка продолжится автоматически.</p>}
    {trade.state === 'open' && <>
      {positions.snapshot.error && <ErrorBox {...describeError(positions.snapshot.error)} />}
      {liveStale && <p className="position-warning" role="status">Снимок позиции не обновляется. PnL и статус защиты могут быть неточными.</p>}
      <p className="freshness">Снимок позиции: {formatDateTime(positions.snapshot.data?.snapshot_captured_at)}</p>
    </>}
    <section className="panel account-summary"><h2 className="summary-label">{trade.state === 'open' ? 'PnL сейчас' : net ? 'Итог сделки' : 'PnL до комиссий'}</h2><div className={`summary-pnl num pnl-${pnl.tone}`}>{pnl.text}</div>
      <div className="summary-row"><span>Всего на вход</span><strong className="num">{formatQuantity(data.entry_qty, trade.quantity_asset)}</strong></div><div className="summary-row"><span>Всего закрыто</span><strong className="num">{formatQuantity(data.exit_qty, trade.quantity_asset)}</strong></div><div className="summary-row"><span>Остаток</span><strong className="num">{formatQuantity(data.remaining_qty, trade.quantity_asset)}</strong></div>
      <div className="summary-row"><span>Средний вход всех входов</span><strong className="num">{formatPrice(data.average_entry)}</strong></div><div className="summary-row"><span>Средний выход</span><strong className="num">{formatPrice(data.average_exit)}</strong></div>
    </section>
    <p className="freshness">Открыта: {formatDateTime(trade.opened_at)}<br />{trade.closed_at && <>Закрыта: {formatDateTime(trade.closed_at)}<br /></>}В позиции {formatDuration(trade.opened_at, trade.closed_at)}</p>
    {trade.quality !== 'complete' && <p className="info-box">Начало истории восстановлено не полностью. Неизвестные цены и объёмы не включены в расчёт.</p>}
    {trade.state === 'open' && (live ? <PositionCard position={live} /> : <p className="info-box">Связь с актуальным снимком позиции уточняется. Статус защиты не подтверждён этим экраном.</p>)}
    <h2 className="h2">Финансовый результат</h2><section className="panel account-summary">
      <div className="summary-row"><span>{trade.state === 'open' ? 'PnL закрытой части до комиссий' : 'PnL до комиссий'}</span><strong className="num">{formatPnl(trade.realized_pnl_gross_usdt).text}</strong></div>
      {data.money && (['commission', 'funding', 'builder'] as const).map(key => { const component = data.money![key]; return <div className="summary-row" key={key}><span>{{ commission: 'Торговые начисления', funding: 'Funding', builder: 'Комиссия Builder' }[key]}</span><strong className="num">{component.state === 'unavailable' ? 'Не проверено' : formatUsdt(component.known_amount)}{component.state === 'partial' ? ' · частично' : ''}</strong></div> })}
      <div className="summary-row"><span>Итог с начислениями</span><strong className="num">{net ? formatPnl(trade.net_pnl_usdt).text : 'Уточняется'}</strong></div>
    </section>
    {data.money?.issues.length ? <ul className="money-issues">{data.money.issues.map(issue => <li key={issue}>{moneyIssue(issue)}</li>)}</ul> : !net && <p className="info-box">Состав начислений ещё не подтверждён.</p>}
    <p className="freshness">Сверка начислений: {formatDateTime(data.money?.as_of)}</p>
    <h2 className="h2">События сделки</h2>
    {!events.data && !events.error && <p role="status" className="info-box">Получаем события и исполнения сделки…</p>}
    {events.data?.events.length === 0 && <p className="info-box">События пока не восстановлены. Это не означает отсутствие исполнений.</p>}
    {events.error && <ErrorBox {...describeError(events.error)} />}
    {events.data?.events.map(event => <TradeEventRow key={event.event_ref} event={event} />)}
    {events.data?.next_events_cursor && <button type="button" className="btn btn-secondary" disabled={events.loading || events.changed} onClick={() => void events.events()}>Ещё события</button>}
    <h2 className="h2">Исполнения</h2>
    {events.data?.fills.length === 0 && <p className="info-box">Исполнения пока недоступны. Объёмы и результат могут уточняться.</p>}
    {groupTradeFills(events.data?.fills ?? []).map(group => <details className="panel fill-details" key={group.key}>
      <summary>{formatDateTime(group.items[0].executed_at)} · {group.items[0].role === 'entry' ? 'Вход' : 'Выход'} · исполнений загружено: {group.items.length}</summary>
      {group.items.map(fill => <div key={`${fill.trade_id}:${fill.role}`}>
        <p>{formatDateTime(fill.executed_at)} · {formatQuantity(fill.allocated_quantity, trade.quantity_asset)} по {formatPrice(fill.price)}</p>
        <p>PnL доли до комиссий: {formatPnl(fill.allocated_realized_pnl).text}</p>
      </div>)}
    </details>)}
    {events.data?.next_fills_cursor && <button type="button" className="btn btn-secondary" disabled={events.loading || events.changed} onClick={() => void events.fills()}>Ещё исполнения</button>}
  </div>
}
