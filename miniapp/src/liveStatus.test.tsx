import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { LiveStatus } from './components/LiveStatus'
import { liveStatusLabel } from './liveStatus'
import type { MiniAppResources } from './resources'

describe('LiveStatus', () => {
  it('keeps healthy state compact and exposes retry only after stale is confirmed', () => {
    expect(liveStatusLabel({ health: 'live', revision: 1, lastUpdatedAt: 1, retrying: false }, 1)).toBe('LIVE')
    expect(liveStatusLabel({ health: 'reconnecting', revision: 1, lastUpdatedAt: 1, retrying: true }, 1)).toBe('Восстанавливаем связь…')
    expect(liveStatusLabel({ health: 'stale', revision: 1, lastUpdatedAt: 100_000, retrying: true }, 145_000)).toBe('Данные устарели · 45 сек назад')

    const retryLive = vi.fn()
    const resources = {
      subscribeLive: () => () => {},
      getLiveSnapshot: () => ({ health: 'stale' as const, revision: 2, lastUpdatedAt: 100_000, retrying: true }),
      retryLive,
    } as unknown as MiniAppResources
    const html = renderToStaticMarkup(<LiveStatus resources={resources} />)
    expect(html).toContain('Данные устарели')
    expect(html).toContain('Повторить')
  })
})
