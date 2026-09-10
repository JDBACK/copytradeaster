import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { LiveStatus } from './components/LiveStatus'
import { liveStatusLabel } from './liveStatus'
import type { MiniAppResources } from './resources'

describe('LiveStatus', () => {
  it('labels only the states a user has to act on', () => {
    expect(liveStatusLabel({ health: 'live', revision: 1, lastUpdatedAt: 1, retrying: false }, 1)).toBe('LIVE')
    expect(liveStatusLabel({ health: 'connecting', revision: 1, lastUpdatedAt: 1, retrying: false }, 1)).toBe('Подключаем live…')
    expect(liveStatusLabel({ health: 'reconnecting', revision: 1, lastUpdatedAt: 1, retrying: true }, 1)).toBe('Восстанавливаем связь…')
    expect(liveStatusLabel({ health: 'stale', revision: 1, lastUpdatedAt: 100_000, retrying: true }, 145_000)).toBe('Нет связи · 45 сек назад')
  })

  it('renders nothing at all while the transport is healthy', () => {
    const resources = {
      subscribeLive: () => () => {},
      getLiveSnapshot: () => ({ health: 'live' as const, revision: 4, lastUpdatedAt: Date.now(), retrying: false }),
      retryLive: vi.fn(),
    } as unknown as MiniAppResources
    expect(renderToStaticMarkup(<LiveStatus resources={resources} />)).toBe('')
  })

  it('stays silent through reconnects and speaks only when the data goes stale', () => {
    // Reconnecting is normal background work; a rail that announces every blip
    // is noise. Only the stale verdict — the channel silent past its deadline —
    // is worth showing, and it carries the age.
    const snapshot = (health: 'live' | 'reconnecting' | 'stale') => ({
      subscribeLive: () => () => {},
      getLiveSnapshot: () => ({ health, revision: 2, lastUpdatedAt: 100_000, retrying: true }),
      retryLive: vi.fn(),
    }) as unknown as MiniAppResources
    expect(renderToStaticMarkup(<LiveStatus resources={snapshot('live')} />)).toBe('')
    expect(renderToStaticMarkup(<LiveStatus resources={snapshot('reconnecting')} />)).toBe('')
  })

  it('announces an outage without asking for a tap', () => {
    const resources = {
      subscribeLive: () => () => {},
      getLiveSnapshot: () => ({ health: 'stale' as const, revision: 2, lastUpdatedAt: 100_000, retrying: true }),
      retryLive: vi.fn(),
    } as unknown as MiniAppResources
    const html = renderToStaticMarkup(<LiveStatus resources={resources} />)
    expect(html).toContain('Нет связи')
    // Reconnects continue in the background; the only manual path is the pull.
    expect(html).not.toContain('<button')
  })
})
