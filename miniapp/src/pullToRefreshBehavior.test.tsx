/** Pull-to-refresh in jsdom: the gesture, the indicator and the one refresh
 * it performs. This is the app's only manual refresh path, so it is asserted
 * end to end against the real shell and the real resource owner. */

// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest'
import { useRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api } from './api'
import { AppShell } from './components/AppShell'
import { PullToRefresh } from './components/PullToRefresh'
import { MiniAppResources } from './resources'
import type { DashboardDTO } from './types'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  delete (window as unknown as { Telegram?: unknown }).Telegram
})

const AUTH = { initData: 'test' }
const noop = () => {}

function dashboard(): DashboardDTO {
  return {
    copy_state: 'active',
    system_state: 'ready',
    account_masked: '0xABC…DEF',
    signer_masked: '0x111…222',
    risk_percent: '0.01',
    open_positions: 0,
    notification_enabled: true,
    is_admin: false,
    snapshot_captured_at: '2026-09-10T00:00:00Z',
    overview: {
      as_of: '2026-09-10T00:00:00Z',
      freshness: 'fresh',
      open_count: 0,
      unrealized_pnl_usdt: '0',
      available_usdt: '100',
      protected_count: 0,
      risk_coverage: 'complete',
    },
  }
}

function Harness({ onRefresh }: { onRefresh: () => Promise<unknown> }) {
  const scroller = useRef<HTMLDivElement | null>(null)
  return (
    <div className="shell">
      <PullToRefresh scroller={scroller} onRefresh={onRefresh} />
      <div className="shell-content" ref={scroller}>
        <p>content</p>
      </div>
    </div>
  )
}

function pull(element: HTMLElement, distance: number) {
  fireEvent.touchStart(element, { touches: [{ clientX: 10, clientY: 100 }] })
  fireEvent.touchMove(element, { touches: [{ clientX: 10, clientY: 100 + distance }] })
}

describe('pull-to-refresh behavior', () => {
  it('stays invisible until the surface is actually pulled', () => {
    const { container } = render(<Harness onRefresh={async () => {}} />)
    const indicator = container.querySelector('.pull-refresh')
    expect(indicator?.getAttribute('data-phase')).toBe('idle')
    expect((indicator as HTMLElement).style.getPropertyValue('--pull-distance')).toBe('0px')
  })

  it('teaches the gesture: distance follows the finger, ready only at the threshold', () => {
    const { container } = render(<Harness onRefresh={async () => {}} />)
    const scroller = container.querySelector('.shell-content') as HTMLElement
    const indicator = container.querySelector('.pull-refresh') as HTMLElement

    pull(scroller, 30)
    expect(indicator.getAttribute('data-phase')).toBe('pulling')
    expect(indicator.style.getPropertyValue('--pull-distance')).toBe('30px')
    expect(screen.queryByText('Отпустите для обновления')).toBeNull()

    fireEvent.touchMove(scroller, { touches: [{ clientX: 10, clientY: 180 }] })
    expect(indicator.getAttribute('data-phase')).toBe('ready')
    expect(screen.getByText('Отпустите для обновления')).toBeDefined()
  })

  it('refreshes once on release past the threshold and folds back afterwards', async () => {
    const onRefresh = vi.fn().mockResolvedValue(undefined)
    const { container } = render(<Harness onRefresh={onRefresh} />)
    const scroller = container.querySelector('.shell-content') as HTMLElement
    const indicator = container.querySelector('.pull-refresh') as HTMLElement

    pull(scroller, 80)
    fireEvent.touchEnd(scroller)
    expect(onRefresh).toHaveBeenCalledTimes(1)
    expect(indicator.getAttribute('data-phase')).toBe('refreshing')
    expect(screen.getByText('Обновляем…')).toBeDefined()

    await waitFor(() => expect(indicator.getAttribute('data-phase')).toBe('idle'), { timeout: 2000 })
    expect(onRefresh).toHaveBeenCalledTimes(1)
  })

  it('a short pull does nothing at all', () => {
    const onRefresh = vi.fn()
    const { container } = render(<Harness onRefresh={onRefresh} />)
    const scroller = container.querySelector('.shell-content') as HTMLElement
    pull(scroller, 20)
    fireEvent.touchEnd(scroller)
    expect(onRefresh).not.toHaveBeenCalled()
    expect((container.querySelector('.pull-refresh') as HTMLElement).getAttribute('data-phase')).toBe('idle')
  })

  it('never arms while the surface is scrolled away from the top', () => {
    const onRefresh = vi.fn()
    const { container } = render(<Harness onRefresh={onRefresh} />)
    const scroller = container.querySelector('.shell-content') as HTMLElement
    scroller.scrollTop = 240
    pull(scroller, 120)
    fireEvent.touchEnd(scroller)
    expect(onRefresh).not.toHaveBeenCalled()
  })

  it('hands the vertical gesture back to Telegram on release', () => {
    const disable = vi.fn()
    const enable = vi.fn()
    ;(window as unknown as { Telegram?: unknown }).Telegram = {
      WebApp: { initData: 't', disableVerticalSwipes: disable, enableVerticalSwipes: enable },
    }
    const { container } = render(<Harness onRefresh={vi.fn()} />)
    const scroller = container.querySelector('.shell-content') as HTMLElement
    pull(scroller, 40)
    expect(disable).toHaveBeenCalled()
    fireEvent.touchEnd(scroller)
    expect(enable).toHaveBeenCalled()
  })

  it('the shell pull re-reads mounted screens authoritatively', async () => {
    const resources = new MiniAppResources(AUTH, 'pull-shell')
    const loader = vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    vi.spyOn(api, 'positions').mockResolvedValue({ positions: [], count: 0, system_state: 'ready', is_stale: false, snapshot_captured_at: null } as never)
    vi.spyOn(api, 'status').mockResolvedValue({} as never)
    const { container } = render(
      <AppShell auth={AUTH} resources={resources} onNotConnected={noop} accountMasked={null} />,
    )
    await waitFor(() => expect(loader).toHaveBeenCalledTimes(1))

    const scroller = container.querySelector('.shell-content') as HTMLElement
    pull(scroller, 90)
    await act(async () => {
      fireEvent.touchEnd(scroller)
    })
    await waitFor(() => expect(loader.mock.calls.length).toBeGreaterThanOrEqual(2), { timeout: 3000 })
    // The pull forces the authoritative read regardless of cache age.
    expect(loader.mock.calls.at(-1)?.[1]).toBe(true)
  })
})
