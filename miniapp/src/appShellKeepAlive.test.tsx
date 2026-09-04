/** Keep-alive tab switching: warm revisits are instant with zero refetch.
 *
 * The AppShell mounts each root tab once (lazy first visit) and hides inactive
 * tabs with `hidden` instead of unmounting. These tests mount the real shell
 * and the real screens in jsdom and assert the live transitions — request
 * counts per leg, preserved filter/page/scroll state, nested overlays that do
 * not unmount their root, the sequence guard, Home partial-ready, and warm
 * revalidation. DTO shapes mirror home.test.ts / positions.test.tsx. */

// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest'
import { act } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { api, ApiError } from './api'
import App from './App'
import { AppShell } from './components/AppShell'
import { ActivityScreen } from './screens/ActivityScreen'
import { HOME_REVALIDATE_MS, HomeScreen } from './screens/HomeScreen'
import { PositionsScreen } from './screens/PositionsScreen'
import type {
  ActivityItemDTO,
  ActivityPageDTO,
  DashboardDTO,
  NotificationSettingsDTO,
  PositionPageDTO,
  PositionSummaryDTO,
  RiskDTO,
} from './types'

// jsdom gaps the production code never meets in Telegram WebView.
if ((globalThis as { CSS?: unknown }).CSS === undefined) {
  ;(globalThis as { CSS?: unknown }).CSS = { escape: (value: string) => value }
}
if (typeof Element !== 'undefined' && typeof Element.prototype.scrollTo !== 'function') {
  Element.prototype.scrollTo = () => {}
}
if (typeof window !== 'undefined' && typeof window.requestAnimationFrame !== 'function') {
  window.requestAnimationFrame = (callback: FrameRequestCallback): number =>
    window.setTimeout(() => callback(performance.now()), 16) as unknown as number
  window.cancelAnimationFrame = (handle: number): void => window.clearTimeout(handle)
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  delete (window as unknown as { Telegram?: unknown }).Telegram
})

const AUTH = { initData: 'test' }
const noop = () => {}

function dashboard(overrides: Partial<DashboardDTO> = {}): DashboardDTO {
  return {
    copy_state: 'paused',
    system_state: 'ready',
    account_masked: '0xABC…DEF',
    signer_masked: '0x111…222',
    risk_percent: '0.01',
    open_positions: 2,
    notification_enabled: true,
    is_admin: false,
    snapshot_captured_at: null,
    ...overrides,
  }
}

function position(overrides: Partial<PositionSummaryDTO> = {}): PositionSummaryDTO {
  return {
    symbol: 'BTCUSDT',
    side: 'LONG',
    qty: '0.050',
    entry_price: '59200.00',
    mark_price: '60150.50',
    unrealized_profit: '47.525',
    leverage: 20,
    protection_state: 'protected',
    stop_price: '58000.00',
    take_profit_price: '64000.00',
    risk_to_stop_usdt: '60.060',
    ...overrides,
  }
}

function positionPage(overrides: Partial<PositionPageDTO> = {}): PositionPageDTO {
  return {
    positions: [position()],
    count: 1,
    system_state: 'ready',
    snapshot_captured_at: '2026-09-02T12:00:00+00:00',
    is_stale: false,
    ...overrides,
  }
}

function activityItem(overrides: Partial<ActivityItemDTO> = {}): ActivityItemDTO {
  return {
    category: 'trade',
    title: 'Открыта позиция BTCUSDT',
    status: 'completed',
    symbol: 'BTCUSDT',
    occurred_at: '2026-09-02T12:41:00+00:00',
    pending_age: null,
    remediation: null,
    ...overrides,
  }
}

function activityPage(overrides: Partial<ActivityPageDTO> = {}): ActivityPageDTO {
  return {
    items: [activityItem()],
    page: 0,
    has_previous: false,
    has_next: false,
    filter: 'all',
    ...overrides,
  }
}

function riskDto(overrides: Partial<RiskDTO> = {}): RiskDTO {
  return {
    risk_percent: '0.01',
    min: '0.005',
    max: '0.05',
    step: '0.005',
    presets: ['0.01', '0.02', '0.03', '0.05'],
    risk_base: 'available_balance',
    ...overrides,
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

/** True when the element sits under a hidden keep-alive wrapper. */
function isHidden(el: Element | null): boolean {
  return !!el?.closest('div[hidden]')
}

function tabButton(name: string): HTMLElement {
  return screen.getByRole('button', { name })
}

describe('AppShell keep-alive', () => {
  it('startup budget is session-free shell: Home 2 + warmup 3, then zero on every leg', async () => {
    const dashboardSpy = vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    const positionsSpy = vi.spyOn(api, 'positions').mockResolvedValue(positionPage())
    const activitySpy = vi.spyOn(api, 'activity').mockResolvedValue(activityPage())
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    await screen.findByText('Риск на стоп')
    // Shell warmup settles: settings and activity prefetched while hidden.
    await screen.findByText('Настройки копирования')
    await screen.findByText('Показаны только данные без секретов')
    // Home dashboard(true) + preview positions(false); warmup adds settings
    // dashboard(false), positions(false) and activity(all, 0).
    expect(dashboardSpy).toHaveBeenCalledTimes(2)
    expect(positionsSpy).toHaveBeenCalledTimes(2)
    expect(activitySpy).toHaveBeenCalledTimes(1)

    // Full second round over all four tabs: zero new requests, no skeletons.
    for (const [tab, marker] of [
      ['Позиции', 'Открытые позиции · 1'],
      ['Активность', 'Показаны только данные без секретов'],
      ['Настройки', 'Настройки копирования'],
      ['Главная', 'Риск на стоп'],
    ] as const) {
      fireEvent.click(tabButton(tab))
      const markerEl = await screen.findByText(marker)
      expect(isHidden(markerEl)).toBe(false)
    }
    expect(screen.queryByLabelText('Загрузка состояния')).toBeNull()
    expect(screen.queryByLabelText('Загрузка позиций')).toBeNull()
    expect(screen.queryByLabelText('Загрузка журнала')).toBeNull()
    expect(screen.queryByLabelText('Загрузка настроек')).toBeNull()
    expect(dashboardSpy).toHaveBeenCalledTimes(2)
    expect(positionsSpy).toHaveBeenCalledTimes(2)
    expect(activitySpy).toHaveBeenCalledTimes(1)
  })

  it('Activity keeps filter, items and page across a Settings detour', async () => {
    const itemA = activityItem({ title: 'Системная проверка A', category: 'system' })
    const itemB = activityItem({ title: 'Сделка B открыта' })
    const itemC = activityItem({ title: 'Сделка C закрыта' })
    const activitySpy = vi
      .spyOn(api, 'activity')
      .mockResolvedValueOnce(activityPage({ items: [itemA], filter: 'all', has_next: false }))
      .mockResolvedValueOnce(activityPage({ items: [itemB], filter: 'trade', has_next: true }))
      .mockResolvedValueOnce(
        activityPage({ items: [itemC], page: 1, filter: 'trade', has_next: false }),
      )
    vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    fireEvent.click(tabButton('Активность'))
    await screen.findByText('Системная проверка A')

    fireEvent.click(screen.getByRole('tab', { name: 'Сделки' }))
    await screen.findByText('Сделка B открыта')

    fireEvent.click(screen.getByRole('button', { name: 'Показать ещё' }))
    await screen.findByText('Сделка C закрыта')
    expect(document.querySelector('[data-activity-page]')?.getAttribute('data-activity-page')).toBe(
      '1',
    )

    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')

    fireEvent.click(tabButton('Активность'))
    await screen.findByText('Сделка B открыта')
    // State survived: appended page, filter selection, no page-0 refetch.
    expect(screen.getByText('Сделка C закрыта')).toBeDefined()
    expect(document.querySelector('[data-activity-page]')?.getAttribute('data-activity-page')).toBe(
      '1',
    )
    expect(screen.getByRole('tab', { name: 'Сделки' }).getAttribute('aria-selected')).toBe('true')
    expect(activitySpy).toHaveBeenCalledTimes(3)
  })

  it('restores the per-tab scroll position on return', async () => {
    vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage())
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    await screen.findByText('Риск на стоп')
    const scroller = document.querySelector('.shell-content') as HTMLElement | null
    expect(scroller).not.toBeNull()
    scroller!.scrollTop = 142

    fireEvent.click(tabButton('Позиции'))
    await screen.findByText('Открытые позиции · 1')

    fireEvent.click(tabButton('Главная'))
    await screen.findByText('Риск на стоп')
    expect(scroller!.scrollTop).toBe(142)
  })

  it('a nested screen hides but never unmounts its root; back needs no refetch', async () => {
    let backHandler: (() => void) | null = null
    ;(window as unknown as { Telegram?: unknown }).Telegram = {
      WebApp: {
        BackButton: {
          show: vi.fn(),
          hide: vi.fn(),
          onClick: vi.fn((cb: () => void) => {
            backHandler = cb
          }),
          offClick: vi.fn(() => {
            backHandler = null
          }),
        },
      },
    }
    const dashboardSpy = vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    vi.spyOn(api, 'risk').mockResolvedValue(riskDto())
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')
    expect(dashboardSpy).toHaveBeenCalledTimes(2)

    fireEvent.click(screen.getByRole('button', { name: /риск на stop/i }))
    const saveButton = await screen.findByRole('button', { name: /сохранить/i })
    expect(isHidden(saveButton)).toBe(false)
    const settingsHeading = screen.getByRole('heading', { name: 'Настройки', hidden: true })
    expect(isHidden(settingsHeading)).toBe(true)
    act(() => {
      backHandler!()
    })
    const restoredHeading = await screen.findByRole('heading', { name: 'Настройки' })
    expect(isHidden(restoredHeading)).toBe(false)
    expect(dashboardSpy).toHaveBeenCalledTimes(2)
  })

  it('nested open saves the root scroll; back restores it, not the nested one', async () => {
    const back = installBackButton()
    vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    vi.spyOn(api, 'risk').mockResolvedValue(riskDto())
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')
    const scroller = document.querySelector('.shell-content') as HTMLElement | null
    expect(scroller).not.toBeNull()
    scroller!.scrollTop = 200

    fireEvent.click(screen.getByRole('button', { name: /риск на stop/i }))
    await screen.findByRole('button', { name: /сохранить/i })
    // Scrolling the nested screen must not leak into the tab slot.
    scroller!.scrollTop = 50

    back.press()
    const restoredHeading = await screen.findByRole('heading', { name: 'Настройки' })
    expect(isHidden(restoredHeading)).toBe(false)
    expect(scroller!.scrollTop).toBe(200)
  })

  it('switching tabs from under nested never writes the nested scroll into a tab slot', async () => {
    installBackButton()
    vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage())
    vi.spyOn(api, 'risk').mockResolvedValue(riskDto())
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')
    const scroller = document.querySelector('.shell-content') as HTMLElement | null
    expect(scroller).not.toBeNull()
    scroller!.scrollTop = 200

    fireEvent.click(screen.getByRole('button', { name: /риск на stop/i }))
    await screen.findByRole('button', { name: /сохранить/i })
    scroller!.scrollTop = 50

    fireEvent.click(tabButton('Позиции'))
    await screen.findByText('Открытые позиции · 1')

    fireEvent.click(tabButton('Настройки'))
    const restoredHeading = await screen.findByRole('heading', { name: 'Настройки' })
    expect(isHidden(restoredHeading)).toBe(false)
    expect(scroller!.scrollTop).toBe(200)
  })
})
function installBackButton(): { press: () => void } {
  let handler: (() => void) | null = null
  ;(window as unknown as { Telegram?: unknown }).Telegram = {
    WebApp: {
      BackButton: {
        show: vi.fn(),
        hide: vi.fn(),
        onClick: vi.fn((cb: () => void) => {
          handler = cb
        }),
        offClick: vi.fn(() => {
          handler = null
        }),
      },
    },
  }
  return {
    press: () => {
      act(() => {
        handler?.()
      })
    },
  }
}


describe('HomeScreen loading lifecycle', () => {
  it('a late first response never overwrites a newer one', async () => {
    const oldPage = deferred<DashboardDTO>()
    const newPage = deferred<DashboardDTO>()
    const dashboardSpy = vi
      .spyOn(api, 'dashboard')
      .mockReturnValueOnce(oldPage.promise)
      .mockReturnValueOnce(newPage.promise)
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))

    const { rerender } = render(
      <HomeScreen auth={{ initData: 'first' }} onNotConnected={noop} />,
    )
    rerender(<HomeScreen auth={{ initData: 'second' }} onNotConnected={noop} />)

    newPage.resolve(dashboard({ open_positions: 7 }))
    await screen.findByText('7')
    oldPage.resolve(dashboard({ open_positions: 2 }))
    // Let the stale generation settle, then assert the winner stayed.
    await act(async () => {})
    expect(screen.getByText('7')).toBeDefined()
    expect(screen.queryByText('2')).toBeNull()
    expect(dashboardSpy).toHaveBeenCalledTimes(2)
  })

  it('paints the dashboard before the positions read resolves', async () => {
    const dashboardGate = deferred<DashboardDTO>()
    const positionsGate = deferred<PositionPageDTO>()
    const dashboardSpy = vi.spyOn(api, 'dashboard').mockReturnValue(dashboardGate.promise)
    const positionsSpy = vi.spyOn(api, 'positions').mockReturnValue(positionsGate.promise)
    render(<HomeScreen auth={AUTH} onNotConnected={noop} />)

    expect(screen.getByLabelText('Загрузка состояния')).toBeDefined()
    dashboardGate.resolve(dashboard())

    // Dashboard UI is already useful while the preview is still pending.
    await screen.findByText('Риск на стоп')
    expect(screen.queryByLabelText('Загрузка состояния')).toBeNull()
    expect(screen.getByLabelText('Загрузка позиций')).toBeDefined()
    expect(screen.queryByText('Активная позиция')).toBeNull()

    positionsGate.resolve(positionPage())
    await screen.findByText('Активная позиция')
    expect(screen.queryByLabelText('Загрузка позиций')).toBeNull()
    // The partial-ready flip must not spawn its own background refresh loop.
    await act(async () => {})
    expect(dashboardSpy).toHaveBeenCalledTimes(1)
    expect(positionsSpy).toHaveBeenCalledTimes(1)
  })
  it('warm activation refetches only when the snapshot aged out', async () => {
    const loadedAt = Date.now()
    const dashboardSpy = vi
      .spyOn(api, 'dashboard')
      .mockResolvedValueOnce(dashboard({ open_positions: 2 }))
      .mockResolvedValue(dashboard({ open_positions: 5 }))
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))

    const { rerender } = render(
      <HomeScreen auth={AUTH} onNotConnected={noop} active={false} />,
    )
    await screen.findByText('Риск на стоп')
    expect(dashboardSpy).toHaveBeenCalledTimes(1)

    // Fresh data: reactivation is free.
    rerender(<HomeScreen auth={AUTH} onNotConnected={noop} active={true} />)
    await screen.findByText('Риск на стоп')
    expect(dashboardSpy).toHaveBeenCalledTimes(1)

    // Aged data: exactly one background refresh, then quiet again.
    rerender(<HomeScreen auth={AUTH} onNotConnected={noop} active={false} />)
    const nowSpy = vi
      .spyOn(Date, 'now')
      .mockReturnValue(loadedAt + HOME_REVALIDATE_MS + 5_000)
    rerender(<HomeScreen auth={AUTH} onNotConnected={noop} active={true} />)
    nowSpy.mockRestore()
    await screen.findByText('5')
    expect(dashboardSpy).toHaveBeenCalledTimes(2)
    expect(dashboardSpy.mock.lastCall?.slice(0, 2)).toEqual([AUTH, true])
  })
})

describe('Part 2A: shell warmup and activation revalidation', () => {
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

  it('full startup budget is session 1 + Home 2 + warmup 3', async () => {
    mockTelegramSession()
    const sessionSpy = vi.spyOn(api, 'session').mockResolvedValue({
      user_id: 777,
      access_state: 'approved',
      environment: 'testnet',
      connection_state: 'connected',
      account_connected: true,
      account_masked: '0xABC…DEF',
    })
    const dashboardSpy = vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    const positionsSpy = vi.spyOn(api, 'positions').mockResolvedValue(positionPage())
    const activitySpy = vi.spyOn(api, 'activity').mockResolvedValue(activityPage())
    render(<App />)

    await screen.findByText('Настройки копирования')
    await screen.findByText('Показаны только данные без секретов')
    expect(sessionSpy).toHaveBeenCalledTimes(1)
    expect(dashboardSpy).toHaveBeenCalledTimes(2)
    expect(positionsSpy).toHaveBeenCalledTimes(2)
    expect(activitySpy).toHaveBeenCalledTimes(1)
  })

  it('warmup waits until Home reaches ready', async () => {
    const dashboardGate = deferred<DashboardDTO>()
    const positionsGate = deferred<PositionPageDTO>()
    const dashboardSpy = vi.spyOn(api, 'dashboard').mockReturnValue(dashboardGate.promise)
    const positionsSpy = vi.spyOn(api, 'positions').mockReturnValue(positionsGate.promise)
    const activitySpy = vi
      .spyOn(api, 'activity')
      .mockResolvedValue(activityPage({ items: [], has_next: false }))
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    await act(async () => {})
    // Home still loading: no snapshot yet, hidden tabs stay quiet.
    expect(dashboardSpy).toHaveBeenCalledTimes(1)
    expect(positionsSpy).toHaveBeenCalledTimes(0)
    expect(activitySpy).toHaveBeenCalledTimes(0)

    dashboardGate.resolve(dashboard())
    await screen.findByText('Риск на стоп')
    positionsGate.resolve(positionPage({ positions: [], count: 0 }))
    await screen.findByText('Настройки копирования')
    expect(dashboardSpy).toHaveBeenCalledTimes(2)
    expect(positionsSpy).toHaveBeenCalledTimes(2)
    expect(activitySpy).toHaveBeenCalledTimes(1)
  })

  it('an early tap fetches immediately and warmup never doubles it', async () => {
    const dashboardGate = deferred<DashboardDTO>()
    vi.spyOn(api, 'dashboard').mockReturnValue(dashboardGate.promise)
    const positionsSpy = vi
      .spyOn(api, 'positions')
      .mockResolvedValue(positionPage({ positions: [], count: 0 }))
    vi.spyOn(api, 'activity').mockResolvedValue(activityPage({ items: [], has_next: false }))
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    // Home still loading, user taps Positions: cold path, no waiting.
    fireEvent.click(tabButton('Позиции'))
    await screen.findByText('Нет открытых позиций')
    expect(positionsSpy).toHaveBeenCalledTimes(1)

    dashboardGate.resolve(dashboard())
    await screen.findByText('Настройки копирования')
    // Home preview added its own read; warmup skipped the visited tab.
    expect(positionsSpy).toHaveBeenCalledTimes(2)
  })

  it('positions activation refetches exactly once past TTL, never within', async () => {
    const t0 = Date.now()
    const positionsSpy = vi
      .spyOn(api, 'positions')
      .mockResolvedValue(positionPage({ positions: [], count: 0 }))
    const { rerender } = render(<PositionsScreen auth={AUTH} />)
    await screen.findByText('Нет открытых позиций')
    expect(positionsSpy).toHaveBeenCalledTimes(1)

    rerender(<PositionsScreen auth={AUTH} active={false} />)
    rerender(<PositionsScreen auth={AUTH} active={true} />)
    await screen.findByText('Нет открытых позиций')
    expect(positionsSpy).toHaveBeenCalledTimes(1)

    rerender(<PositionsScreen auth={AUTH} active={false} />)
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(t0 + HOME_REVALIDATE_MS + 5_000)
    rerender(<PositionsScreen auth={AUTH} active={true} />)
    nowSpy.mockRestore()
    await screen.findByText('Нет открытых позиций')
    expect(positionsSpy).toHaveBeenCalledTimes(2)
    expect(positionsSpy.mock.lastCall?.slice(0, 2)).toEqual([AUTH, false])
  })

  it('activity keeps its 60 s floor: 35 s is free, 65 s refetches once', async () => {
    const t0 = Date.now()
    const activitySpy = vi
      .spyOn(api, 'activity')
      .mockResolvedValue(activityPage({ items: [], has_next: false }))
    const { rerender } = render(<ActivityScreen auth={AUTH} />)
    await screen.findByText('Безопасный журнал действий')
    expect(activitySpy).toHaveBeenCalledTimes(1)

    const bounce = async (ageMs: number) => {
      rerender(<ActivityScreen auth={AUTH} active={false} />)
      const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(t0 + ageMs)
      rerender(<ActivityScreen auth={AUTH} active={true} />)
      nowSpy.mockRestore()
      await screen.findByText('Безопасный журнал действий')
    }
    await bounce(35_000)
    expect(activitySpy).toHaveBeenCalledTimes(1)
    await bounce(65_000)
    expect(activitySpy).toHaveBeenCalledTimes(2)
  })

  it('revalidation past TTL never resets appended pagination', async () => {
    const t0 = Date.now()
    const activitySpy = vi
      .spyOn(api, 'activity')
      .mockResolvedValueOnce(
        activityPage({ items: [activityItem()], has_next: true }),
      )
      .mockResolvedValueOnce(
        activityPage({ items: [activityItem({ title: 'Сделка B открыта' })], page: 1, has_next: false }),
      )
    const { rerender } = render(<ActivityScreen auth={AUTH} />)
    await screen.findByText('Открыта позиция BTCUSDT')

    // Paginate to page 1, then age past the TTL and come back.
    fireEvent.click(screen.getByRole('button', { name: 'Показать ещё' }))
    await screen.findByText('Сделка B открыта')
    rerender(<ActivityScreen auth={AUTH} active={false} />)
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(t0 + 65_000 + 1)
    rerender(<ActivityScreen auth={AUTH} active={true} />)
    nowSpy.mockRestore()
    await screen.findByText('Сделка B открыта')
    // The paginated timeline is preserved verbatim: no page-0 refresh fired.
    expect(screen.getByText('Открыта позиция BTCUSDT')).toBeDefined()
    expect(activitySpy).toHaveBeenCalledTimes(2)
  })

  it('activation never fires while the first fetch is still in flight', async () => {
    const gate = deferred<PositionPageDTO>()
    const positionsSpy = vi.spyOn(api, 'positions').mockReturnValue(gate.promise)
    const { rerender } = render(<PositionsScreen auth={AUTH} active={true} />)
    expect(screen.getByLabelText('Загрузка позиций')).toBeDefined()

    rerender(<PositionsScreen auth={AUTH} active={false} />)
    rerender(<PositionsScreen auth={AUTH} active={true} />)
    await act(async () => {})
    expect(positionsSpy).toHaveBeenCalledTimes(1)

    gate.resolve(positionPage({ positions: [], count: 0 }))
    await screen.findByText('Нет открытых позиций')
    expect(positionsSpy).toHaveBeenCalledTimes(1)
  })

  it('failed warmup shows error UI with retry, activation past TTL retries', async () => {
    const positionsSpy = vi
      .spyOn(api, 'positions')
      .mockRejectedValueOnce(new ApiError(500, 'network_error', 'down'))
      .mockResolvedValue(positionPage({ positions: [], count: 0 }))
    const { rerender } = render(<PositionsScreen auth={AUTH} active={false} warm={false} />)
    // Gated: the loading frame shows, but no request leaves.
    expect(screen.getByLabelText('Загрузка позиций')).toBeDefined()
    expect(positionsSpy).toHaveBeenCalledTimes(0)
    // Warmup fires and fails while hidden: error UI waits for the opener.
    rerender(<PositionsScreen auth={AUTH} active={false} warm={true} />)
    await screen.findByRole('button', { name: 'Повторить' })
    expect(positionsSpy).toHaveBeenCalledTimes(1)

    // Opening the tab past TTL retries automatically and lands ready.
    const t0 = Date.now()
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(t0 + HOME_REVALIDATE_MS + 5_000)
    rerender(<PositionsScreen auth={AUTH} active={true} warm={true} />)
    nowSpy.mockRestore()
    await screen.findByText('Нет открытых позиций')
    expect(positionsSpy).toHaveBeenCalledTimes(2)
  })
})

describe('Corrective: mutation-driven invalidation', () => {
  function notificationsDto(
    overrides: Partial<NotificationSettingsDTO> = {},
  ): NotificationSettingsDTO {
    return { enabled: true, last_delivery_at: null, pending: 0, failed: 0, ...overrides }
  }

  it('Risk save stales Home and Settings: instant old UI, one background refresh, new value', async () => {
    const back = installBackButton()
    const bgGate = deferred<DashboardDTO>()
    const dashboardSpy = vi
      .spyOn(api, 'dashboard')
      .mockResolvedValueOnce(dashboard({ risk_percent: '0.01' }))
      .mockResolvedValueOnce(dashboard({ risk_percent: '0.01' }))
      .mockReturnValue(bgGate.promise)
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    vi.spyOn(api, 'activity').mockResolvedValue(activityPage({ items: [], has_next: false }))
    vi.spyOn(api, 'risk').mockResolvedValue(riskDto())
    const setter = vi
      .spyOn(api, 'setRiskPercent')
      .mockResolvedValue(riskDto({ risk_percent: '0.02' }))
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')
    expect(dashboardSpy).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('button', { name: /риск на stop.*1%/i })).toBeDefined()

    fireEvent.click(screen.getByRole('button', { name: /риск на stop/i }))
    await screen.findByRole('button', { name: /сохранить/i })
    fireEvent.click(screen.getByRole('radio', { name: '2,0%' }))
    fireEvent.click(screen.getByRole('button', { name: /сохранить 2,0%/i }))
    await screen.findByText(/сохранено · 2,0%/i)
    expect(setter).toHaveBeenCalledTimes(1)

    back.press()
    const restoredHeading = await screen.findByRole('heading', { name: 'Настройки' })
    expect(isHidden(restoredHeading)).toBe(false)
    // Old UI instantly: exactly one background refresh, hanging on the gate.
    await act(async () => {})
    expect(screen.queryByLabelText('Загрузка настроек')).toBeNull()
    expect(dashboardSpy).toHaveBeenCalledTimes(3)
    expect(dashboardSpy.mock.lastCall?.slice(0, 2)).toEqual([AUTH, false])
    expect(screen.getByRole('button', { name: /риск на stop.*1%/i })).toBeDefined()

    bgGate.resolve(dashboard({ risk_percent: '0.02' }))
    await screen.findByRole('button', { name: /риск на stop.*2%/i })
    // Settles quiet: invalidation fires once, never loops.
    await act(async () => {})
    await act(async () => {})
    expect(dashboardSpy).toHaveBeenCalledTimes(3)

    // Home was staled by the same save: one background dashboard(true).
    fireEvent.click(tabButton('Главная'))
    await screen.findByText('Риск на стоп')
    await act(async () => {})
    expect(dashboardSpy).toHaveBeenCalledTimes(4)
    expect(dashboardSpy.mock.lastCall?.slice(0, 2)).toEqual([AUTH, true])
  })

  it('Notifications save stales Settings: one background refresh on return', async () => {
    const back = installBackButton()
    const bgGate = deferred<DashboardDTO>()
    const dashboardSpy = vi
      .spyOn(api, 'dashboard')
      .mockResolvedValueOnce(dashboard())
      .mockResolvedValueOnce(dashboard())
      .mockReturnValue(bgGate.promise)
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    vi.spyOn(api, 'activity').mockResolvedValue(activityPage({ items: [], has_next: false }))
    vi.spyOn(api, 'notifications').mockResolvedValue(notificationsDto())
    const saver = vi
      .spyOn(api, 'setNotifications')
      .mockResolvedValue(notificationsDto({ enabled: false }))
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')
    expect(dashboardSpy).toHaveBeenCalledTimes(2)

    fireEvent.click(screen.getByRole('button', { name: /уведомления/i }))
    const toggle = await screen.findByRole('switch', { name: 'Уведомления о сделках' })
    fireEvent.click(toggle)
    await act(async () => {})
    expect(toggle.getAttribute('aria-checked')).toBe('false')
    expect(saver).toHaveBeenCalledTimes(1)

    back.press()
    await screen.findByRole('heading', { name: 'Настройки' })
    await act(async () => {})
    expect(screen.queryByLabelText('Загрузка настроек')).toBeNull()
    expect(dashboardSpy).toHaveBeenCalledTimes(3)
    bgGate.resolve(dashboard())
    await act(async () => {})
    expect(dashboardSpy).toHaveBeenCalledTimes(3)
  })

  it('Home pause stales Settings and Activity despite a fresh TTL', async () => {
    const dashboardSpy = vi
      .spyOn(api, 'dashboard')
      .mockResolvedValue(dashboard({ copy_state: 'active' }))
    const activitySpy = vi
      .spyOn(api, 'activity')
      .mockResolvedValue(activityPage({ items: [], has_next: false }))
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    const pauser = vi
      .spyOn(api, 'pause')
      .mockResolvedValue(dashboard({ copy_state: 'paused' }))
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    fireEvent.click(await screen.findByRole('button', { name: /поставить на паузу/i }))
    await screen.findByRole('button', { name: /возобновить/i })
    expect(pauser).toHaveBeenCalledTimes(1)

    // Fresh TTL everywhere, yet Settings revalidates on the mutation signal.
    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')
    await act(async () => {})
    expect(screen.queryByLabelText('Загрузка настроек')).toBeNull()
    expect(dashboardSpy).toHaveBeenCalledTimes(3)

    fireEvent.click(tabButton('Активность'))
    await screen.findByText('Показаны только данные без секретов')
    await act(async () => {})
    expect(activitySpy).toHaveBeenCalledTimes(2)
  })
})

describe('Corrective: single-flight background revalidation', () => {
  it('rapid Home reactivation while a background refresh hangs starts no second request', async () => {
    const gate = deferred<DashboardDTO>()
    const dashboardSpy = vi
      .spyOn(api, 'dashboard')
      .mockResolvedValueOnce(dashboard())
      .mockReturnValue(gate.promise)
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))

    const { rerender } = render(<HomeScreen auth={AUTH} onNotConnected={noop} active={true} />)
    await screen.findByText('Риск на стоп')
    expect(dashboardSpy).toHaveBeenCalledTimes(1)

    const t0 = Date.now()
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(t0 + HOME_REVALIDATE_MS + 5_000)
    rerender(<HomeScreen auth={AUTH} onNotConnected={noop} active={false} />)
    rerender(<HomeScreen auth={AUTH} onNotConnected={noop} active={true} />)
    await act(async () => {})
    expect(dashboardSpy).toHaveBeenCalledTimes(2)

    // Bounce again while the refresh hangs: single-flight swallows it.
    rerender(<HomeScreen auth={AUTH} onNotConnected={noop} active={false} />)
    rerender(<HomeScreen auth={AUTH} onNotConnected={noop} active={true} />)
    await act(async () => {})
    expect(dashboardSpy).toHaveBeenCalledTimes(2)

    // Settle, age again: the next refresh is allowed.
    nowSpy.mockRestore()
    gate.resolve(dashboard({ open_positions: 5 }))
    await screen.findByText('5')
    const t1 = Date.now()
    const nowSpy2 = vi.spyOn(Date, 'now').mockReturnValue(t1 + HOME_REVALIDATE_MS + 5_000)
    rerender(<HomeScreen auth={AUTH} onNotConnected={noop} active={false} />)
    rerender(<HomeScreen auth={AUTH} onNotConnected={noop} active={true} />)
    nowSpy2.mockRestore()
    await act(async () => {})
    expect(dashboardSpy).toHaveBeenCalledTimes(3)
  })

  it('Positions bounces while its background refresh hangs: still one request', async () => {
    const gate = deferred<PositionPageDTO>()
    const positionsSpy = vi
      .spyOn(api, 'positions')
      .mockResolvedValueOnce(positionPage({ positions: [], count: 0 }))
      .mockReturnValue(gate.promise)
    const { rerender } = render(<PositionsScreen auth={AUTH} active={true} warm={true} />)
    await screen.findByText('Нет открытых позиций')
    expect(positionsSpy).toHaveBeenCalledTimes(1)

    const t0 = Date.now()
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(t0 + HOME_REVALIDATE_MS + 5_000)
    rerender(<PositionsScreen auth={AUTH} active={false} warm={true} />)
    rerender(<PositionsScreen auth={AUTH} active={true} warm={true} />)
    await act(async () => {})
    expect(positionsSpy).toHaveBeenCalledTimes(2)
    rerender(<PositionsScreen auth={AUTH} active={false} warm={true} />)
    rerender(<PositionsScreen auth={AUTH} active={true} warm={true} />)
    await act(async () => {})
    expect(positionsSpy).toHaveBeenCalledTimes(2)
    nowSpy.mockRestore()

    gate.resolve(positionPage({ positions: [], count: 0 }))
    await act(async () => {})
    expect(positionsSpy).toHaveBeenCalledTimes(2)
  })
})

describe('Corrective: nested screens open at the top', () => {
  it('nested opens scrolled to top; back restores the root position', async () => {
    const back = installBackButton()
    vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    vi.spyOn(api, 'risk').mockResolvedValue(riskDto())
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')
    const scroller = document.querySelector('.shell-content') as HTMLElement | null
    expect(scroller).not.toBeNull()
    scroller!.scrollTop = 300

    fireEvent.click(screen.getByRole('button', { name: /риск на stop/i }))
    await screen.findByRole('button', { name: /сохранить/i })
    expect(scroller!.scrollTop).toBe(0)

    scroller!.scrollTop = 150
    back.press()
    const restoredHeading = await screen.findByRole('heading', { name: 'Настройки' })
    expect(isHidden(restoredHeading)).toBe(false)
    expect(scroller!.scrollTop).toBe(300)
  })
})
