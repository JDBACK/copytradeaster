/** User facts, never journal-command success interpreted as a fill. */
import { useEffect, useMemo, useState } from 'react'
import type { AuthContext } from '../api'
import type { MiniAppResources } from '../resources'
import { useLiveSnapshot, useScreenResources } from '../useResource'
import { railSpeaks } from '../live'
import { useResourceScope } from '../resourceScopeContext'
import { useHistory } from '../useHistory'
import { usePaging } from '../usePaging'
import type { ActivityFilter } from '../types'
import type { NestedScreen } from '../navigation'
import { ACTIVITY_TIME_LABELS, dayBucket } from '../activityView'
import { Brand, ErrorBox } from '../components/ui'
import { TradeEventRow } from '../components/TradeEventRow'
import { describeError } from '../errors'
import { hapticImpact } from '../telegram'

export function ActivityScreen({ auth, resources: providedResources, accountMasked, active = true, warm = true, invalidateSignal = 0, foregroundSignal = 0, onOpen = () => {} }: { auth: AuthContext; resources?: MiniAppResources; accountMasked?: string | null; active?: boolean; warm?: boolean; invalidateSignal?: number; foregroundSignal?: number; onOpen?: (route: NestedScreen) => void }) {
  const scope = useResourceScope()
  const resources = useScreenResources(providedResources ?? scope ?? undefined, auth)
  const [filter, setFilter] = useState<ActivityFilter>('all')
  const [paging, startPaging] = usePaging()
  const [calendar, setCalendar] = useState(() => new Date())
  const calendarDay = calendar.toDateString()
  const feed = useHistory(resources.history, { kind: 'feed', filter }, active || (warm && !resources.history.get({ kind: 'feed', filter }).data), foregroundSignal + invalidateSignal)
  const railOwnsOutage = railSpeaks(useLiveSnapshot(resources), feed.data !== null)
  useEffect(() => {
    if (!active) return
    // Local midnight updates labels only, never triggers an API request.
    const update = () => setCalendar(new Date())
    update()
    const now = new Date()
    const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1)
    const timer = setTimeout(update, midnight.getTime() - now.getTime() + 50)
    return () => clearTimeout(timer)
  }, [active, foregroundSignal, calendarDay])
  const groups = useMemo(() => (['today', 'yesterday', 'earlier'] as const).map(bucket => ({ bucket, items: feed.data?.items.filter(item => dayBucket(item.occurred_at, calendar) === bucket) ?? [] })), [feed.data?.items, calendar])
  return <div className="screen">
    <Brand right={accountMasked ? <span className="wallet-chip num">{accountMasked}</span> : undefined} />
    <h1 className="h1">Активность</h1>
    <div className="segmented" role="tablist" aria-label="Фильтр журнала">{(['all', 'trade', 'system'] as const).map(value => <button type="button" role="tab" key={value} className={`segmented-item${filter === value ? ' active' : ''}`} aria-selected={filter === value} onClick={() => { setFilter(value); hapticImpact('light') }}>{value === 'all' ? 'Все' : value === 'trade' ? 'Сделки' : 'Система'}</button>)}</div>
    {!!feed.data?.pending.length && <section className="panel"><h2 className="h2">В процессе</h2>{feed.data.pending.map((operation, index) => <p key={`${operation.symbol}:${operation.operation}:${index}`}>{operation.state === 'UNKNOWN' ? 'Проверяем результат на бирже' : 'Ожидаем подтверждения'} {operation.symbol ?? ''}</p>)}</section>}
    {feed.error && !railOwnsOutage && <ErrorBox {...describeError(feed.error)} />}
    {!feed.data && !feed.error && <div className="skeleton" style={{ minHeight: 240 }} aria-label="Загрузка журнала" />}
    {feed.data?.items.length === 0 && <p className="info-box">{filter === 'trade' ? 'Сделок пока нет' : filter === 'system' ? 'Системных событий пока нет' : 'Событий пока нет'}</p>}
    {groups.filter(group => group.items.length).map(group => <section key={group.bucket} aria-label={ACTIVITY_TIME_LABELS[group.bucket]}><h2 className="activity-day-label">{ACTIVITY_TIME_LABELS[group.bucket]}</h2>{group.items.map(event => <TradeEventRow key={event.event_ref} event={event} onOpen={tradeRef => onOpen({ kind: 'trade-detail', tradeRef })} />)}</section>)}
    {feed.data?.next_cursor && <button type="button" className="btn btn-secondary" disabled={paging} onClick={() => startPaging(() => feed.more())}>{paging ? 'Загружаем…' : 'Показать ещё'}</button>}
  </div>
}
