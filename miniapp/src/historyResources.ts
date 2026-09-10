/** Bounded history DTOs owned by one connected session. No persistence. */
import { api, ApiError, type AuthContext } from './api'
import type { MiniAppResources } from './resources'
import type { ActivityFeedPage, ActivityFilter, TradeResponseDTO, TradeEventsPage, TradeListPage, TradePeriod } from './types'
export interface HistoryValues { trades: TradeListPage; feed: ActivityFeedPage; detail: TradeResponseDTO; events: TradeEventsPage }
export type HistoryQuery =
  | { kind: 'trades'; period: TradePeriod; symbol?: string; limit?: number }
  | { kind: 'feed'; filter: ActivityFilter }
  | { kind: 'detail'; tradeRef: string }
  | { kind: 'events'; tradeRef: string }
export interface HistorySnapshot<T> { data: T | null; loading: boolean; error: string | null; receivedAt: number; changed: boolean }
interface Entry {
  firstPage?: string
  failedMode?: LoadMode
  query: HistoryQuery
  snapshot: HistorySnapshot<HistoryValues[keyof HistoryValues]>
  listeners: Set<() => void>
  pins: number
  used: number
  generation: number
  revision: number
  /** One silent re-base is allowed per loaded slice: a correction can re-cut
   * the page under the user, and that is not an error they caused. */
  rebased: boolean
  flightRevision?: number
  followUp: boolean
  controller: AbortController | null
  flight: Promise<void> | null
}
export const historyKey = (query: HistoryQuery) => JSON.stringify(query)
type LoadMode = 'ensure' | 'refresh' | 'more' | 'fills' | 'events'
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
      entry = { query, snapshot: { data: null, loading: false, error: null, receivedAt: 0, changed: false }, listeners: new Set(), pins: 0, used: Date.now(), generation: 0, revision: 0, followUp: false, rebased: false, controller: null, flight: null }
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
  retry(query: HistoryQuery): Promise<void> {
    const entry = this.entry(query)
    return this.load(query, entry.snapshot.changed ? 'refresh' : entry.failedMode ?? 'refresh')
  }
  async load(query: HistoryQuery, mode: LoadMode = 'ensure'): Promise<void> {
    if (this.disposed) return
    const entry = this.entry(query)
    // A rejected revision cannot be extended or silently replaced by a TTL
    // read. Keep the displayed slice until the user explicitly restarts it.
    if (entry.snapshot.changed && mode !== 'refresh') return
    if (entry.flight) {
      if (entry.flightRevision !== entry.revision) entry.followUp = true
      return entry.flight
    }
    const ttl = query.kind === 'feed' ? 60000 : 30000
    if (mode === 'ensure' && entry.snapshot.data && entry.snapshot.receivedAt > 0 && Date.now() - entry.snapshot.receivedAt < ttl) return
    const controller = new AbortController()
    entry.controller = controller
    const generation = ++entry.generation
    const revision = entry.revision
    entry.flightRevision = revision
    entry.followUp = false
    const previous = entry.snapshot.data
    this.publish(entry, { loading: true, error: null })
    const run = async () => {
      try {
        let data: HistoryValues[keyof HistoryValues]
        let firstPage: string | undefined
        const options = { signal: controller.signal }
        if (query.kind === 'trades') {
          const old = previous as TradeListPage | null
          const cursor = mode === 'more' ? old?.next_cursor : null
          if (mode === 'more' && !cursor) return
          const page = await api.trades(this.auth, { period: query.period, symbol: query.symbol || undefined, limit: query.limit ?? 20, cursor }, options)
          const signature = JSON.stringify(page.items)
          if (cursor && old) {
            data = { ...page, items: merge(old.items, page.items, 'trade_ref') }
          } else if (old && entry.firstPage === signature) {
            // Same first page: keep the loaded slice and its continuation.
            data = old
          } else {
            // A first-page read (initial load, live invalidation, tab
            // activation or explicit refresh). A changed list is good news:
            // the new trade simply appears. The previous behaviour raised
            // `history_page_changed` and demanded a manual reload, which turned
            // every ordinary update into an error the user had to clear by hand.
            data = page
            firstPage = signature
          }
        } else if (query.kind === 'feed') {
          const old = previous as ActivityFeedPage | null
          const cursor = mode === 'more' ? old?.next_cursor : null
          if (mode === 'more' && !cursor) return
          const page = await api.activityFeed(this.auth, query.filter, cursor, options)
          const signature = JSON.stringify(page.items)
          // Continuations append to the loaded slice; a first-page read adopts
          // the server's fresh pin, so new events simply appear instead of
          // asking the user to tap "there are new events". Late events never
          // interleave either way: the pin is server-side.
          if (cursor && old) {
            data = { ...page, items: merge(old.items, page.items, 'event_ref') }
          } else if (old && entry.firstPage === signature) {
            data = { ...old, pending: page.pending, latest_seq: page.latest_seq }
          } else {
            data = page
            firstPage = signature
          }
        } else if (query.kind === 'detail') data = await api.trade(this.auth, query.tradeRef, options)
        else {
          const old = previous as TradeEventsPage | null
          const moreFills = mode === 'fills' && old?.next_fills_cursor
          const moreEvents = mode === 'events' && old?.next_events_cursor
          if ((mode === 'fills' && !moreFills) || (mode === 'events' && !moreEvents)) return
          const page = await api.tradeEvents(this.auth, query.tradeRef, { fills_cursor: moreFills || null, events_cursor: moreEvents || null }, options)
          const signature = JSON.stringify([page.fills, page.events])
          if (old && (moreFills || moreEvents)) {
            data = {
              ...old,
              ...(moreFills ? { fills: merge(old.fills, page.fills, item => `${item.trade_id}:${item.role}`), next_fills_cursor: page.next_fills_cursor } : {}),
              ...(moreEvents ? { events: merge(old.events, page.events, 'event_ref'), next_events_cursor: page.next_events_cursor } : {}),
            }
          } else if (old && entry.firstPage === signature) {
            data = old
          } else {
            data = page
            firstPage = signature
          }
        }
        if (!this.disposed && generation === entry.generation && revision === entry.revision) {
          entry.failedMode = undefined
          entry.rebased = false
          if (firstPage !== undefined) entry.firstPage = firstPage
          this.publish(entry, { data, receivedAt: Date.now(), changed: false })
        }
      } catch (error) {
        if (this.owner.handleInvalidSessionError(error)) return
        if (this.disposed || generation !== entry.generation || revision !== entry.revision || (error instanceof ApiError && error.code === 'aborted')) return
        const code = error instanceof ApiError ? error.code : 'network_error'
        // A live correction can re-cut the page the user is reading. Re-base the
        // slice silently once instead of asking them to clear an error they did
        // not cause and cannot interpret; only a second failure surfaces.
        if (code === 'history_page_changed' && !entry.rebased) {
          entry.rebased = true
          entry.failedMode = undefined
          // Force the re-base to adopt the server's fresh pin instead of
          // matching the slice that was just rejected.
          entry.firstPage = undefined
          const target = entry
          const attempted = revision
          // Deferred: the `finally` below owns clearing this flight, so the
          // re-base must not race it for the single-flight slot.
          setTimeout(() => {
            if (this.disposed || target.revision !== attempted) return
            void this.load(query, 'refresh')
          }, 0)
          return
        }
        entry.failedMode = mode
        this.publish(entry, { error: code, changed: code === 'history_page_changed', ...(code === 'trade_unavailable' ? { data: null } : {}) })
      } finally {
        if (generation === entry.generation) {
          entry.flight = null
          entry.controller = null
          this.publish(entry, { loading: false })
          this.prune()
          if (!this.disposed && revision !== entry.revision && entry.followUp) {
            await this.load(query, 'refresh')
          }
        }
      }
    }
    entry.flight = run()
    return entry.flight
  }
  invalidate(): void {
    if (this.disposed) return
    for (const entry of this.entries.values()) {
      entry.revision++
      this.publish(entry, { receivedAt: 0 })
    }
  }
  liveInvalidate(): void {
    if (this.disposed) return
    for (const entry of this.entries.values()) {
      entry.revision++
      this.publish(entry, { receivedAt: 0 })
      if (entry.listeners.size > 0) void this.load(entry.query, 'refresh')
    }
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
