import { afterEach, expect, it, vi } from 'vitest'
import { api } from './api'
import { MiniAppResources } from './resources'
import type { ActivityFeedPage } from './types'

const query = { kind: 'feed', filter: 'all' } as const
const page = (seq: number): ActivityFeedPage => ({
  items: [], next_cursor: null, high_water: seq, latest_seq: seq, pending: [],
})

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

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
