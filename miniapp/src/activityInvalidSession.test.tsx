// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest'
import { act } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import App from './App'
import { api, ApiError } from './api'
import { ActivityScreen } from './screens/ActivityScreen'
import { MiniAppResources } from './resources'
import { ResourceScopeContext } from './resourceScopeContext'
import type { ActivityPageDTO, DashboardDTO, PositionPageDTO } from './types'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  delete (window as unknown as { Telegram?: unknown }).Telegram
})

function mockTelegramSession() {
  ;(window as unknown as { Telegram?: unknown }).Telegram = {
    WebApp: {
      initData: 'test-init',
      ready: vi.fn(),
      expand: vi.fn(),
      setHeaderColor: vi.fn(),
      setBackgroundColor: vi.fn(),
    },
  }
}

const dashboard: DashboardDTO = {
  copy_state: 'paused',
  system_state: 'ready',
  account_masked: '0xABC…DEF',
  risk_percent: '0.01',
  open_positions: 0,
  notification_enabled: true,
  is_admin: false,
  snapshot_captured_at: '2026-09-07T12:00:00+00:00',
}

const positions: PositionPageDTO = {
  positions: [],
  count: 0,
  system_state: 'ready',
  snapshot_captured_at: '2026-09-07T12:00:00+00:00',
  is_stale: false,
}

function activityPage(title: string): ActivityPageDTO {
  return {
    items: [
      {
        category: 'system',
        title,
        status: 'completed',
        symbol: null,
        occurred_at: '2026-09-07T12:01:00+00:00',
        pending_age: null,
        remediation: null,
      },
    ],
    page: 0,
    has_previous: false,
    has_next: false,
    filter: 'all',
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

describe('Activity direct invalid-session boundary', () => {
  it.each([
    new ApiError(401, 'unauthorized', 'expired'),
    new ApiError(500, 'invalid_session', 'expired'),
  ])('routes %s to the App invalid gate with one Activity request', async (error) => {
    mockTelegramSession()
    vi.spyOn(api, 'session').mockResolvedValue({
      user_id: 777,
      access_state: 'approved',
      environment: 'testnet',
      connection_state: 'connected',
      account_connected: true,
      account_masked: '0xABC…DEF',
    })
    vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard)
    vi.spyOn(api, 'positions').mockResolvedValue(positions)
    const activity = vi.spyOn(api, 'activity').mockRejectedValue(error)

    render(<App />)

    await screen.findByRole('heading', { name: 'Откройте из бота' })
    expect(activity).toHaveBeenCalledTimes(1)
    expect(screen.getAllByRole('heading', { name: 'Откройте из бота' })).toHaveLength(1)
  })

  it('replaces Activity truth on resource-scope change and fences a late old success', async () => {
    const firstScope = new MiniAppResources({ initData: 'test' }, 'activity-scope-old')
    const secondScope = new MiniAppResources({ initData: 'test' }, 'activity-scope-new')
    const oldGate = deferred<ActivityPageDTO>()
    const oldSignals: AbortSignal[] = []
    const activity = vi
      .spyOn(api, 'activity')
      .mockImplementationOnce((_auth, _filter, _page, options) => {
        if (options?.signal) oldSignals.push(options.signal)
        return oldGate.promise
      })
      .mockResolvedValueOnce(activityPage('NEW SCOPE EVENT'))

    const { rerender } = render(
      <ResourceScopeContext.Provider value={firstScope}>
        <ActivityScreen auth={{ initData: 'test' }} />
      </ResourceScopeContext.Provider>,
    )
    await waitFor(() => expect(activity).toHaveBeenCalledTimes(1))
    expect(screen.getByLabelText('Загрузка журнала')).toBeDefined()

    rerender(
      <ResourceScopeContext.Provider value={secondScope}>
        <ActivityScreen auth={{ initData: 'test' }} />
      </ResourceScopeContext.Provider>,
    )

    await waitFor(() => expect(activity).toHaveBeenCalledTimes(2))
    expect(oldSignals).toHaveLength(1)
    expect(oldSignals[0]?.aborted).toBe(true)
    await screen.findByText('NEW SCOPE EVENT')

    await act(async () => {
      oldGate.resolve(activityPage('OLD SCOPE EVENT'))
      await Promise.resolve()
    })

    expect(screen.queryByText('OLD SCOPE EVENT')).toBeNull()
    expect(screen.getByText('NEW SCOPE EVENT')).toBeDefined()
    expect(activity).toHaveBeenCalledTimes(2)
  })
})
