/** Bounded history DTOs owned by one connected session. No persistence. */
import { api, ApiError, type AuthContext } from './api'
import type { MiniAppResources } from './resources'
import type { ActivityFeedPage, ActivityFilter, TradeDetailDTO, TradeEventsPage, TradeListPage, TradePeriod } from './types'
export interface HistoryValues { trades: TradeListPage; feed: ActivityFeedPage; detail: TradeDetailDTO; events: TradeEventsPage }
export type HistoryQuery =
  | { kind: 'trades'; period: TradePeriod; symbol?: string; limit?: number }
  | { kind: 'feed'; filter: ActivityFilter }
  | { kind: 'detail'; tradeRef: string }
  | { kind: 'events'; tradeRef: string }
export interface HistorySnapshot<T> { data: T | null; loading: boolean; error: string | null; receivedAt: number; changed: boolean }
interface Entry {
  query: HistoryQuery
  snapshot: HistorySnapshot<HistoryValues[keyof HistoryValues]>
  listeners: Set<() => void>
  pins: number
  used: number
  generation: number
  controller: AbortController | null
  flight: Promise<void> | null
}
export const historyKey = (query: HistoryQuery) => JSON.stringify(query)
export class HistoryResources {
  private entries = new Map<string, Entry>()
  private disposed = false
  private readonly auth: AuthContext
  private readonly owner: MiniAppResources
  constructor(auth: AuthContext, owner: MiniAppResources) { this.auth = auth; this.owner = owner }
  private entry(query: HistoryQuery): Entry {
    const key = historyKey(query)
    let entry = this.entries.get(key)
    if (!entry) {
      entry = { query, snapshot: { data: null, loading: false, error: null, receivedAt: 0, changed: false }, listeners: new Set(), pins: 0, used: Date.now(), generation: 0, controller: null, flight: null }
      this.entries.set(key, entry)
    }
    entry.used = Date.now()
    return entry
  }
  get<Q extends HistoryQuery>(query: Q): HistorySnapshot<HistoryValues[Q['kind']]> {
    return this.entry(query).snapshot as HistorySnapshot<HistoryValues[Q['kind']]>
  }
  subscribe(query: HistoryQuery, listener: () => void): () => void {
    const entry = this.entry(query)
    entry.listeners.add(listener)
    return () => {
      entry.listeners.delete(listener)
      queueMicrotask(() => {
        if (!entry.listeners.size && !entry.pins) {
          entry.generation++
          entry.controller?.abort()
          entry.controller = null
          entry.flight = null
          this.publish(entry, { loading: false })
          this.prune()
        }
      })
    }
  }
  pin(query: HistoryQuery): () => void {
    const entry = this.entry(query)
    entry.pins++
    return () => { entry.pins--; this.prune() }
  }
  private publish(entry: Entry, patch: Partial<Entry['snapshot']>) {
    entry.snapshot = { ...entry.snapshot, ...patch }
    entry.listeners.forEach(listener => listener())
  }
  async load(query: HistoryQuery, mode: 'ensure' | 'refresh' | 'more' | 'fills' | 'events' = 'ensure'): Promise<void> {
    if (this.disposed) return
    const entry = this.entry(query)
    if (entry.flight) return entry.flight
    if (mode === 'ensure' && entry.snapshot.data && Date.now() - entry.snapshot.receivedAt < 30000) return
    const controller = new AbortController()
    entry.controller = controller
    const generation = ++entry.generation
    const previous = entry.snapshot.data
    this.publish(entry, { loading: true, error: null })
    const run = async () => {
      try {
        let data: HistoryValues[keyof HistoryValues]
        const options = { signal: controller.signal }
        if (query.kind === 'trades') {
          const old = previous as TradeListPage | null
          const cursor = mode === 'more' ? old?.next_cursor : null
          if (mode === 'more' && !cursor) return
          const page = await api.trades(this.auth, { period: query.period, symbol: query.symbol || undefined, limit: query.limit ?? 20, cursor }, options)
          data = cursor && old ? { ...page, items: merge(old.items, page.items, 'trade_ref') } : page
        } else if (query.kind === 'feed') {
          const old = previous as ActivityFeedPage | null
          const cursor = mode === 'more' ? old?.next_cursor : null
          if (mode === 'more' && !cursor) return
          const page = await api.activityFeed(this.auth, query.filter, cursor, options)
          // Automatic freshness probes preserve the pinned slice and offer an
          // explicit new-events reset; never interleave a late event.
          if (mode === 'ensure' && old) {
            data = { ...old, pending: page.pending, latest_seq: page.latest_seq }
          } else data = cursor && old ? { ...page, items: merge(old.items, page.items, 'event_ref') } : page
        } else if (query.kind === 'detail') data = await api.trade(this.auth, query.tradeRef, options)
        else {
          const old = previous as TradeEventsPage | null
          const moreFills = mode === 'fills' && old?.next_fills_cursor
          const moreEvents = mode === 'events' && old?.next_events_cursor
          if ((mode === 'fills' && !moreFills) || (mode === 'events' && !moreEvents)) return
          const page = await api.tradeEvents(this.auth, query.tradeRef, { fills_cursor: moreFills || null, events_cursor: moreEvents || null }, options)
          data = old && (moreFills || moreEvents) ? {
            ...old,
            ...(moreFills ? { fills: merge(old.fills, page.fills, item => `${item.trade_id}:${item.role}`), next_fills_cursor: page.next_fills_cursor } : {}),
            ...(moreEvents ? { events: merge(old.events, page.events, 'event_ref'), next_events_cursor: page.next_events_cursor } : {}),
          } : page
        }
        if (!this.disposed && generation === entry.generation) this.publish(entry, { data, receivedAt: Date.now(), changed: false })
      } catch (error) {
        if (this.owner.handleInvalidSessionError(error)) return
        if (this.disposed || generation !== entry.generation || (error instanceof ApiError && error.code === 'aborted')) return
        const code = error instanceof ApiError ? error.code : 'network_error'
        this.publish(entry, { error: code, changed: code === 'history_page_changed' })
      } finally {
        if (generation === entry.generation) {
          entry.flight = null
          entry.controller = null
          this.publish(entry, { loading: false })
          this.prune()
        }
      }
    }
    entry.flight = run()
    return entry.flight
  }
  invalidate(): void {
    for (const entry of this.entries.values()) this.publish(entry, { receivedAt: 0 })
  }
  dispose(): void {
    this.disposed = true
    for (const entry of this.entries.values()) {
      entry.generation++
      entry.controller?.abort()
      this.publish(entry, { data: null, loading: false, error: null })
    }
    this.entries.clear()
  }
  private prune(): void {
    const inactive = [...this.entries.entries()].filter(([, e]) => !e.listeners.size && !e.pins && !e.flight).sort((a, b) => b[1].used - a[1].used)
    let lists = 0, details = 0
    for (const [key, entry] of inactive) {
      const list = entry.query.kind === 'trades' || entry.query.kind === 'feed'
      if (list ? ++lists > 3 : ++details > 10) { this.entries.delete(key); continue }
      if (list && entry.snapshot.data && 'items' in entry.snapshot.data && entry.snapshot.data.items.length > 100) {
        // Evict the whole inactive slice, never truncate a cursor or active scroll.
        this.entries.delete(key)
      }
    }
  }
}
function merge<T>(old: T[], next: T[], key: keyof T | ((item: T) => string)): T[] {
  const id = (item: T) => typeof key === 'function' ? key(item) : item[key]
  const seen = new Set(old.map(id))
  return [...old, ...next.filter(item => !seen.has(id(item)) && !!seen.add(id(item)))]
}
