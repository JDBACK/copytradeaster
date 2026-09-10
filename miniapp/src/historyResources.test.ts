import { afterEach, expect, it, vi } from 'vitest'
import { api, ApiError } from './api'
import { MiniAppResources } from './resources'
import type { ActivityFeedPage, TradeEventsPage, TradeListItem, TradeListPage } from './types'

const query = { kind: 'feed', filter: 'all' } as const
const page = (seq: number): ActivityFeedPage => ({
  items: [], next_cursor: null, high_water: seq, latest_seq: seq, pending: [],
})

it('auto-rebases a re-cut page instead of demanding a manual reload', async () => {
  const first = { ...page(1), next_cursor: 'old-cursor' }
  const read = vi.spyOn(api, 'activityFeed').mockResolvedValueOnce(first)
    .mockRejectedValueOnce(new ApiError(409, 'history_page_changed', 'revision changed'))
    .mockResolvedValueOnce(page(2))
  const owner = new MiniAppResources({ initData: 'test' }, 'revision-test')
  await owner.history.load(query)
  await owner.history.load(query, 'more')
  // A correction re-cuts the page under the user. That is not their error and
  // must not become one: exactly one silent re-base follows, no error state and
  // no button to press.
  await vi.waitFor(() => expect(read).toHaveBeenCalledTimes(3))
  expect(owner.history.get(query).data?.high_water).toBe(2)
  expect(owner.history.get(query).changed).toBe(false)
  expect(owner.history.get(query).error).toBeNull()
  owner.dispose()
})

it('reports a failed visible revalidation instead of resolving early', async () => {
  const owner = new MiniAppResources({ initData: 'test' }, 'revalidate-failure')
  const read = vi
    .spyOn(api, 'activityFeed')
    .mockRejectedValue(new ApiError(0, 'network_error', 'down'))
  const unsubscribe = owner.history.subscribe(query, () => {})

  // Hidden: the query is only marked stale, so there is nothing to report.
  await expect(owner.history.revalidateActive()).resolves.toBeUndefined()
  expect(read).not.toHaveBeenCalled()

  // Visible: the read is part of the truth the pull-down reports on.
  owner.history.setActive(query, true)
  await expect(owner.history.revalidateActive()).rejects.toThrow('history refresh failed')
  expect(read).toHaveBeenCalledTimes(1)

  owner.history.setActive(query, false)
  unsubscribe()
  owner.dispose()
})

it('retries the failed events cursor without dropping previous pages or moving the fills cursor', async () => {
  const event = (ref: string) => ({ event_ref: ref, text: ref, kind: 'OPENED',
    trade_ref: 'trade', occurred_at: '2026-09-08T00:00:00Z', facts: {} })
  const first: TradeEventsPage = { events: [event('a')], fills: [], next_cursor: null,
    fills_cursor: null, events_cursor: null, next_fills_cursor: 'fills-1', next_events_cursor: 'events-1' }
  const read = vi.spyOn(api, 'tradeEvents').mockResolvedValueOnce(first)
    .mockRejectedValueOnce(new ApiError(0, 'network_error', 'lost'))
    .mockResolvedValueOnce({ ...first, events: [event('b')], next_events_cursor: null,
      next_fills_cursor: 'must-not-replace' })
  const owner = new MiniAppResources({ initData: 'test' }, 'retry-test')
  const query = { kind: 'events', tradeRef: 'trade' } as const
  await owner.history.load(query)
  await owner.history.load(query, 'events')
  expect(owner.history.get(query).data?.events.map(item => item.event_ref)).toEqual(['a'])
  await owner.history.retry(query)
  expect(read.mock.calls[2]?.[2]).toEqual({ fills_cursor: null, events_cursor: 'events-1' })
  expect(owner.history.get(query).data?.events.map(item => item.event_ref)).toEqual(['a', 'b'])
  expect(owner.history.get(query).data?.next_fills_cursor).toBe('fills-1')
  expect(owner.history.get(query).error).toBeNull()
  owner.dispose()
})

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

it('evicts older inactive filters while retaining a mounted Back target', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-09-08T00:00:00Z'))
  const read = vi.spyOn(api, 'trades').mockResolvedValue({ items: [], next_cursor: null,
    as_of: null, coverage_from: null, coverage_complete: false })
  const owner = new MiniAppResources({ initData: 'test' }, 'bounded-lists')
  const query = (symbol: string) => ({ kind: 'trades', period: 'all', symbol } as const)
  const unsubscribe = owner.history.subscribe(query('BACK'), () => {})
  await owner.history.load(query('BACK'))
  for (const symbol of ['A', 'B', 'C', 'D']) {
    vi.advanceTimersByTime(1)
    await owner.history.load(query(symbol))
  }
  read.mockClear()
  for (const symbol of ['BACK', 'B', 'C', 'D']) await owner.history.load(query(symbol))
  expect(read).not.toHaveBeenCalled()
  await owner.history.load(query('A'))
  expect(read).toHaveBeenCalledTimes(1)
  unsubscribe()
  owner.dispose()
})

it('keeps a large active slice intact and evicts it as a whole after release', async () => {
  const items = Array.from({ length: 101 }, (_, index) => ({ event_ref: String(index),
    text: 'Event', kind: 'OPENED', trade_ref: null,
    occurred_at: '2026-09-08T00:00:00Z', facts: {} }))
  const read = vi.spyOn(api, 'activityFeed').mockResolvedValue({ ...page(101), items })
  const owner = new MiniAppResources({ initData: 'test' }, 'large-feed')
  const unsubscribe = owner.history.subscribe(query, () => {})
  await owner.history.load(query)
  expect(owner.history.get(query).data?.items).toHaveLength(101)
  unsubscribe()
  await Promise.resolve()
  await owner.history.load(query)
  expect(read).toHaveBeenCalledTimes(2)
  owner.dispose()
})

it('keeps a continuation while the page holds and adopts it silently when it moves', async () => {
  const trade = (ref: string): TradeListItem => ({ trade_ref: ref, symbol: 'BTCUSDT',
    quantity_asset: 'BTC', side: 'LONG', state: 'closed', source: 'copy',
    opened_at: null, closed_at: null, executed_qty: '1', realized_pnl_gross_usdt: '0',
    net_pnl_usdt: null, quality: 'complete', result_state: 'gross_only', money: null,
    average_entry: '100', average_exit: '100' })
  const first: TradeListPage = { items: [trade('a')], next_cursor: 'c1',
    as_of: null, coverage_from: null, coverage_complete: false }
  vi.spyOn(api, 'trades').mockResolvedValueOnce(first)
    .mockResolvedValueOnce({ ...first, items: [trade('a'), trade('b')], next_cursor: 'c2' })
    .mockResolvedValueOnce(first)
    .mockResolvedValue({ ...first, items: [trade('new')], next_cursor: null })
  const owner = new MiniAppResources({ initData: 'test' }, 'trade-pages')
  const query = { kind: 'trades', period: 'all' } as const
  await owner.history.load(query)
  await owner.history.load(query, 'more')
  owner.history.invalidate()
  await owner.history.load(query)
  expect(owner.history.get(query).data?.items.map(item => item.trade_ref)).toEqual(['a', 'b'])
  expect(owner.history.get(query).data?.next_cursor).toBe('c2')
  owner.history.invalidate()
  await owner.history.load(query)
  expect(owner.history.get(query).data?.items.map(item => item.trade_ref)).toEqual(['new'])
  expect(owner.history.get(query).data?.next_cursor).toBeNull()
  expect(owner.history.get(query).changed).toBe(false)
  expect(owner.history.get(query).error).toBeNull()
  owner.dispose()
})

it('preserves appended detail events on a TTL probe and adopts a corrected first page silently', async () => {
  const event = (ref: string) => ({ event_ref: ref, text: ref, kind: 'OPENED',
    trade_ref: 'trade', occurred_at: '2026-09-08T00:00:00Z', facts: {} })
  const first: TradeEventsPage = { events: [event('a')], fills: [], next_cursor: null,
    fills_cursor: null, events_cursor: null, next_fills_cursor: 'f1', next_events_cursor: 'e1' }
  const read = vi.spyOn(api, 'tradeEvents').mockResolvedValueOnce(first)
    .mockResolvedValueOnce({ ...first, events: [event('b')], next_events_cursor: 'e2' })
    .mockResolvedValueOnce(first)
    .mockResolvedValueOnce({ ...first, events: [event('corrected')] })
  const owner = new MiniAppResources({ initData: 'test' }, 'ttl-pages')
  const query = { kind: 'events', tradeRef: 'trade' } as const
  await owner.history.load(query)
  await owner.history.load(query, 'events')
  owner.history.invalidate()
  await owner.history.load(query)
  expect(owner.history.get(query).data?.events.map(item => item.event_ref)).toEqual(['a', 'b'])
  expect(owner.history.get(query).data?.next_events_cursor).toBe('e2')
  expect(owner.history.get(query).changed).toBe(false)
  owner.history.invalidate()
  await owner.history.load(query)
  expect(owner.history.get(query).changed).toBe(false)
  expect(owner.history.get(query).error).toBeNull()
  expect(owner.history.get(query).data?.events.map(item => item.event_ref)).toEqual(['corrected'])
  expect(read).toHaveBeenCalledTimes(4)
  owner.dispose()
})

it('keeps the Activity freshness floor at sixty seconds', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-09-08T00:00:00Z'))
  const read = vi.spyOn(api, 'activityFeed').mockResolvedValue(page(1))
  const owner = new MiniAppResources({ initData: 'test' }, 'history-ttl')
  await owner.history.load(query)
  vi.advanceTimersByTime(35000)
  await owner.history.load(query)
  expect(read).toHaveBeenCalledTimes(1)
  vi.advanceTimersByTime(30000)
  await owner.history.load(query)
  expect(read).toHaveBeenCalledTimes(2)
  owner.dispose()
})

it('fences a pre-invalidation response and joins one requested follow-up', async () => {
  let resolve!: (value: ActivityFeedPage) => void
  const old = new Promise<ActivityFeedPage>(done => { resolve = done })
  const read = vi.spyOn(api, 'activityFeed').mockReturnValueOnce(old).mockResolvedValue(page(2))
  const owner = new MiniAppResources({ initData: 'test' }, 'history-test')
  const first = owner.history.load(query)
  owner.history.invalidate()
  const second = owner.history.load(query)
  const third = owner.history.load(query)
  resolve(page(1))
  await Promise.all([first, second, third])
  expect(read).toHaveBeenCalledTimes(2)
  expect(owner.history.get(query).data?.high_water).toBe(2)
  owner.dispose()
})

it('keeps an invalidated inactive read stale without starting a hidden follow-up', async () => {
  let resolve!: (value: ActivityFeedPage) => void
  const read = vi.spyOn(api, 'activityFeed').mockImplementationOnce(() =>
    new Promise(done => { resolve = done }))
  const owner = new MiniAppResources({ initData: 'test' }, 'history-test')
  const first = owner.history.load(query)
  owner.history.invalidate()
  resolve(page(1))
  await first
  expect(read).toHaveBeenCalledTimes(1)
  expect(owner.history.get(query).data).toBeNull()
  expect(owner.history.get(query).receivedAt).toBe(0)
  owner.dispose()
})
