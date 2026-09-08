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
import { act, StrictMode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api, ApiError } from './api'
import App from './App'
import { AppShell } from './components/AppShell'
import { ActivityScreen } from './screens/ActivityScreen'
import { ACTIVITY_REVALIDATE_MS } from './revalidate'
import { HOME_REVALIDATE_MS, HomeScreen } from './screens/HomeScreen'
import { PositionsScreen } from './screens/PositionsScreen'
import { SettingsScreen } from './screens/SettingsScreen'
import { MiniAppResources } from './resources'
import type {
  AccountBuilderDTO,
  ActivityItemDTO,
  ActivityPageDTO,
  ActivityFeedPage,
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
    overview: {
      as_of: '2026-09-08T00:00:00Z', freshness: 'fresh',
      open_count: overrides.open_positions ?? 2, unrealized_pnl_usdt: '0',
      available_usdt: '100', protected_count: overrides.open_positions ?? 2,
      risk_coverage: 'complete',
    },
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

function activityPage(overrides: Partial<ActivityPageDTO> = {}): ActivityFeedPage {
  return {
    items: (overrides.items ?? [activityItem()]).map(item => ({
      event_ref: item.title, text: item.title,
      kind: item.category === 'system' ? 'SYSTEM_STATUS' : 'OPENED',
      trade_ref: null,
      occurred_at: item.occurred_at ?? '2026-09-02T12:41:00+00:00', facts: {},
    })),
    next_cursor: overrides.has_next ? 'next-page' : null,
    high_water: 10, latest_seq: 10, pending: [],
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

function accountBuilderDto(overrides: Partial<AccountBuilderDTO> = {}): AccountBuilderDTO {
  return {
    identity_source: 'builder',
    state: 'verified',
    reason: 'none',
    remediation: null,
    account_masked: '0xABC…DEF',
    signer_masked: '0x111…222',
    builder_address_masked: '0x2576…6EF8',
    builder_name: 'copytradeaster',
    max_fee_rate: '0.0001',
    permission: { can_read: true, can_perp_trade: true, can_withdraw: false },
    agent_expires_at_ms: 2_000_000_000_000,
    verified_at: '2026-09-03T12:00:00+00:00',
    last_check_at: '2026-09-03T12:00:00+00:00',
    age_seconds: 0,
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
  it('startup budget is session-free shell: one shared dashboard + positions + activity', async () => {
    const dashboardSpy = vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    const positionsSpy = vi.spyOn(api, 'positions').mockResolvedValue(positionPage())
    const activitySpy = vi.spyOn(api, 'activityFeed').mockResolvedValue(activityPage())
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    await screen.findByRole('heading', { name: 'PnL открытых позиций' })
    // Shell warmup settles: settings and activity prefetched while hidden.
    await screen.findByText('Настройки копирования')
    await screen.findByText('Обновить события')
    // S2 resource sharing: Settings joins Home's dashboard and Positions joins
    // Home's preview resource instead of starting duplicate reads.
    expect(dashboardSpy).toHaveBeenCalledTimes(1)
    expect(positionsSpy).toHaveBeenCalledTimes(1)
    expect(activitySpy).toHaveBeenCalledTimes(1)

    // Full second round over all four tabs: zero new requests, no skeletons.
    for (const [tab, marker] of [
      ['Позиции', 'Открытые 1'],
      ['Активность', 'Обновить события'],
      ['Настройки', 'Настройки копирования'],
      ['Главная', 'PnL открытых позиций'],
    ] as const) {
      fireEvent.click(tabButton(tab))
      const markerEl = await screen.findByText(marker)
      expect(isHidden(markerEl)).toBe(false)
    }
    expect(screen.queryByLabelText('Загрузка состояния')).toBeNull()
    expect(screen.queryByLabelText('Загрузка позиций')).toBeNull()
    expect(screen.queryByLabelText('Загрузка журнала')).toBeNull()
    expect(screen.queryByLabelText('Загрузка настроек')).toBeNull()
    expect(dashboardSpy).toHaveBeenCalledTimes(1)
    expect(positionsSpy).toHaveBeenCalledTimes(1)
    expect(activitySpy).toHaveBeenCalledTimes(1)
  })

  it('Activity keeps filter, items and page across a Settings detour', async () => {
    const itemA = activityItem({ title: 'Системная проверка A', category: 'system' })
    const itemB = activityItem({ title: 'Сделка B открыта' })
    const itemC = activityItem({ title: 'Сделка C закрыта' })
    const activitySpy = vi
      .spyOn(api, 'activityFeed')
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
    expect(screen.queryByRole('button', { name: 'Показать ещё' })).toBeNull()
    expect(activitySpy.mock.calls[2]?.[2]).toBe('next-page')

    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')

    fireEvent.click(tabButton('Активность'))
    await screen.findByText('Сделка B открыта')
    // State survived: appended page, filter selection, no page-0 refetch.
    expect(screen.getByText('Сделка C закрыта')).toBeDefined()
    expect(screen.queryByRole('button', { name: 'Показать ещё' })).toBeNull()
    expect(activitySpy.mock.calls[2]?.[2]).toBe('next-page')
    expect(screen.getByRole('tab', { name: 'Сделки' }).getAttribute('aria-selected')).toBe('true')
    expect(activitySpy).toHaveBeenCalledTimes(3)
  })

  it('restores the per-tab scroll position on return', async () => {
    vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage())
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    await screen.findByRole('heading', { name: 'PnL открытых позиций' })
    const scroller = document.querySelector('.shell-content') as HTMLElement | null
    expect(scroller).not.toBeNull()
    scroller!.scrollTop = 142

    fireEvent.click(tabButton('Позиции'))
    await screen.findByText('Открытые 1')

    fireEvent.click(tabButton('Главная'))
    await screen.findByRole('heading', { name: 'PnL открытых позиций' })
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
    expect(dashboardSpy).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: /риск на сделку/i }))
    const saveButton = await screen.findByRole('button', { name: /сохранить/i })
    expect(isHidden(saveButton)).toBe(false)
    const settingsHeading = screen.getByRole('heading', { name: 'Настройки', hidden: true })
    expect(isHidden(settingsHeading)).toBe(true)
    act(() => {
      backHandler!()
    })
    const restoredHeading = await screen.findByRole('heading', { name: 'Настройки' })
    expect(isHidden(restoredHeading)).toBe(false)
    expect(dashboardSpy).toHaveBeenCalledTimes(1)
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

    fireEvent.click(screen.getByRole('button', { name: /риск на сделку/i }))
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

    fireEvent.click(screen.getByRole('button', { name: /риск на сделку/i }))
    await screen.findByRole('button', { name: /сохранить/i })
    scroller!.scrollTop = 50

    fireEvent.click(tabButton('Позиции'))
    await screen.findByText('Открытые 1')

    fireEvent.click(tabButton('Настройки'))
    const restoredHeading = await screen.findByRole('heading', { name: 'Настройки' })
    expect(isHidden(restoredHeading)).toBe(false)
    expect(scroller!.scrollTop).toBe(200)
  })

  it('StrictMode reaches ready with one shared dashboard, positions and activity read', async () => {
    const dashboardSpy = vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    const positionsSpy = vi.spyOn(api, 'positions').mockResolvedValue(positionPage())
    const activitySpy = vi.spyOn(api, 'activityFeed').mockResolvedValue(activityPage())

    render(
      <StrictMode>
        <AppShell auth={AUTH} onNotConnected={noop} />
      </StrictMode>,
    )

    await screen.findByText('Настройки копирования')
    await screen.findByText('Обновить события')
    expect(screen.queryByLabelText('Загрузка состояния')).toBeNull()
    expect(dashboardSpy).toHaveBeenCalledTimes(1)
    expect(positionsSpy).toHaveBeenCalledTimes(1)
    expect(activitySpy).toHaveBeenCalledTimes(1)
  })

  it('reopens cached nested Risk inside TTL without another API read', async () => {
    const back = installBackButton()
    vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    vi.spyOn(api, 'activityFeed').mockResolvedValue(activityPage({ items: [], has_next: false }))
    const riskSpy = vi.spyOn(api, 'risk').mockResolvedValue(riskDto())
    vi.spyOn(api, 'notifications').mockResolvedValue({
      enabled: true,
      last_delivery_at: null,
      pending: 0,
      failed: 0,
    })

    render(<AppShell auth={AUTH} onNotConnected={noop} />)
    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')

    fireEvent.click(screen.getByRole('button', { name: /риск на сделку/i }))
    await screen.findByRole('button', { name: /сохранить/i })
    expect(riskSpy).toHaveBeenCalledTimes(1)
    back.press()
    await screen.findByRole('heading', { name: 'Настройки' })

    fireEvent.click(screen.getByRole('button', { name: /риск на сделку/i }))
    await screen.findByRole('button', { name: /сохранить/i })
    expect(riskSpy).toHaveBeenCalledTimes(1)
  })

  it('dirty Risk prompts on Back and tab leave, while explicit discard leaves without saving', async () => {
    const back = installBackButton()
    vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    vi.spyOn(api, 'activityFeed').mockResolvedValue(activityPage({ items: [], has_next: false }))
    vi.spyOn(api, 'risk').mockResolvedValue(riskDto())
    const setter = vi.spyOn(api, 'setRiskPercent')

    render(<AppShell auth={AUTH} onNotConnected={noop} />)
    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')
    fireEvent.click(screen.getByRole('button', { name: /риск на сделку/i }))
    await screen.findByRole('button', { name: /сохранить/i })
    fireEvent.click(screen.getByRole('radio', { name: '2,0%' }))

    back.press()
    expect(await screen.findByText('Отменить изменения?')).toBeDefined()
    // Native Back obeys modal stack order: close the discard Sheet first,
    // keep the dirty Risk screen underneath, then a later Back may ask again.
    back.press()
    expect(screen.queryByText('Отменить изменения?')).toBeNull()
    expect(screen.getByRole('button', { name: /сохранить 2,0%/i })).toBeDefined()
    back.press()
    expect(await screen.findByText('Отменить изменения?')).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Остаться' }))
    expect(screen.queryByText('Отменить изменения?')).toBeNull()

    fireEvent.click(tabButton('Позиции'))
    expect(await screen.findByText('Отменить изменения?')).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Выйти без сохранения' }))
    await screen.findByText('Нет открытых позиций')
    expect(setter).not.toHaveBeenCalled()
  })

  it('can leave during an in-flight Risk save and re-entry cannot start a second POST', async () => {
    const back = installBackButton()
    const saveGate = deferred<RiskDTO>()
    vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    vi.spyOn(api, 'activityFeed').mockResolvedValue(activityPage({ items: [], has_next: false }))
    vi.spyOn(api, 'risk').mockResolvedValue(riskDto())
    const setter = vi.spyOn(api, 'setRiskPercent').mockReturnValue(saveGate.promise)

    render(<AppShell auth={AUTH} onNotConnected={noop} />)
    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')
    fireEvent.click(screen.getByRole('button', { name: /риск на сделку/i }))
    await screen.findByRole('button', { name: /сохранить/i })
    fireEvent.click(screen.getByRole('radio', { name: '2,0%' }))
    fireEvent.click(screen.getByRole('button', { name: /сохранить 2,0%/i }))
    expect(setter).toHaveBeenCalledTimes(1)

    // The session-scoped mutation lock intentionally outlives this nested
    // component. Back is allowed while the write is already admitted.
    back.press()
    await screen.findByRole('heading', { name: 'Настройки' })
    expect(screen.queryByText('Отменить изменения?')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /риск на сделку/i }))
    const savingButton = await screen.findByRole('button', { name: /сохранение/i })
    expect(savingButton).toBeDisabled()
    fireEvent.click(savingButton)
    expect(setter).toHaveBeenCalledTimes(1)

    saveGate.resolve(riskDto({ risk_percent: '0.02' }))
    await screen.findByText(/сейчас 2,0%/i)
    expect(setter).toHaveBeenCalledTimes(1)
  })

  it('can leave during an in-flight Notifications save and remount cannot duplicate it', async () => {
    const back = installBackButton()
    const saveGate = deferred<NotificationSettingsDTO>()
    vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    vi.spyOn(api, 'activityFeed').mockResolvedValue(activityPage({ items: [], has_next: false }))
    vi.spyOn(api, 'notifications').mockResolvedValue({
      enabled: true,
      last_delivery_at: null,
      pending: 0,
      failed: 0,
    })
    const setter = vi.spyOn(api, 'setNotifications').mockReturnValue(saveGate.promise)

    render(<AppShell auth={AUTH} onNotConnected={noop} />)
    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')
    fireEvent.click(screen.getByRole('button', { name: /уведомления/i }))
    const toggle = await screen.findByRole('switch', { name: 'Уведомления о сделках' })
    fireEvent.click(toggle)
    expect(setter).toHaveBeenCalledTimes(1)

    back.press()
    await screen.findByRole('heading', { name: 'Настройки' })
    fireEvent.click(screen.getByRole('button', { name: /уведомления/i }))
    const reopened = await screen.findByRole('switch', { name: 'Уведомления о сделках' })
    expect(reopened).toBeDisabled()
    fireEvent.click(reopened)
    expect(setter).toHaveBeenCalledTimes(1)

    saveGate.resolve({ enabled: false, last_delivery_at: null, pending: 0, failed: 0 })
    await act(async () => {})
    expect(screen.getByRole('switch', { name: 'Уведомления о сделках' })).toHaveAttribute(
      'aria-checked',
      'false',
    )
    expect(setter).toHaveBeenCalledTimes(1)
  })

  it('native Back closes AccountBuilder confirmation Sheet before leaving the nested screen', async () => {
    const back = installBackButton()
    vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    vi.spyOn(api, 'activityFeed').mockResolvedValue(activityPage({ items: [], has_next: false }))
    vi.spyOn(api, 'accountBuilder').mockResolvedValue(accountBuilderDto())
    vi.spyOn(api, 'beginConfirmation').mockResolvedValue({
      token: 'disconnect-token',
      action: 'disconnect',
      account_id: 'copyc1',
      expires_at: null,
    })

    render(<AppShell auth={AUTH} onNotConnected={noop} />)
    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')
    fireEvent.click(screen.getByRole('button', { name: /подключение aster/i }))
    await screen.findByText('Builder подтверждён')
    fireEvent.click(screen.getByRole('button', { name: 'Отключить аккаунт' }))
    await screen.findByText('Отключить аккаунт?')

    back.press()
    expect(screen.queryByText('Отключить аккаунт?')).toBeNull()
    expect(screen.getByRole('heading', { name: 'Подключение Aster' })).toBeDefined()

    back.press()
    await screen.findByRole('heading', { name: 'Настройки' })
  })

  it('deactivates a Home confirmation Sheet when its keep-alive root is hidden', async () => {
    vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard({ copy_state: 'paused' }))
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    vi.spyOn(api, 'activityFeed').mockResolvedValue(activityPage({ items: [], has_next: false }))
    vi.spyOn(api, 'beginConfirmation').mockResolvedValue({
      token: 'resume-token',
      action: 'resume',
      account_id: 'copyc1',
      expires_at: null,
    })
    const resume = vi.spyOn(api, 'resume')

    render(<AppShell auth={AUTH} onNotConnected={noop} />)
    fireEvent.click(await screen.findByRole('button', { name: /возобновить/i }))
    await screen.findByText('Возобновить копирование?')
    expect(document.body.style.overflow).toBe('hidden')

    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')
    await act(async () => {})
    expect(screen.queryByText('Возобновить копирование?')).toBeNull()
    expect(document.body.style.overflow).toBe('')

    fireEvent.click(tabButton('Главная'))
    await screen.findByRole('button', { name: /возобновить/i })
    expect(screen.queryByText('Возобновить копирование?')).toBeNull()
    expect(resume).not.toHaveBeenCalled()
  })

  it('50 Settings → Risk → Back cycles keep DOM and API ownership bounded', async () => {
    const back = installBackButton()
    const resources = new MiniAppResources(AUTH, 'lifecycle-50')
    const dashboardSpy = vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    const positionsSpy = vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    const activitySpy = vi.spyOn(api, 'activityFeed').mockResolvedValue(activityPage({ items: [], has_next: false }))
    const riskSpy = vi.spyOn(api, 'risk').mockResolvedValue(riskDto())
    const notificationsSpy = vi.spyOn(api, 'notifications').mockResolvedValue({
      enabled: true,
      last_delivery_at: null,
      pending: 0,
      failed: 0,
    })
    const statusSpy = vi.spyOn(api, 'status').mockResolvedValue({} as never)
    const accountSpy = vi.spyOn(api, 'accountBuilder').mockResolvedValue({} as never)

    render(<AppShell auth={AUTH} resources={resources} onNotConnected={noop} />)
    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')
    // Let the bounded Settings prefetch pass settle before taking the baseline.
    await act(async () => {})
    await act(async () => {})

    fireEvent.click(screen.getByRole('button', { name: /риск на сделку/i }))
    await screen.findByRole('button', { name: /сохранить/i })
    back.press()
    await screen.findByRole('heading', { name: 'Настройки' })

    const baselineNodes = document.querySelectorAll('*').length
    const baselineCalls = () => [
      dashboardSpy.mock.calls.length,
      positionsSpy.mock.calls.length,
      activitySpy.mock.calls.length,
      riskSpy.mock.calls.length,
      notificationsSpy.mock.calls.length,
      statusSpy.mock.calls.length,
      accountSpy.mock.calls.length,
    ]
    const calls = baselineCalls()

    for (let i = 0; i < 50; i += 1) {
      fireEvent.click(screen.getByRole('button', { name: /риск на сделку/i }))
      expect(screen.getByRole('button', { name: /сохранить/i })).toBeDefined()
      back.press()
      expect(screen.getByRole('heading', { name: 'Настройки' })).toBeDefined()
    }

    expect(baselineCalls()).toEqual(calls)
    expect(document.querySelectorAll('*').length).toBe(baselineNodes)
    expect(document.body.style.overflow).toBe('')
    for (const key of [
      'dashboard',
      'positions',
      'risk',
      'notifications',
      'status',
      'accountBuilder',
    ] as const) {
      expect(resources.getSnapshot(key).inflight).toBe(false)
    }
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
  it('keeps pause locked across remount until an ambiguous timeout is authoritatively reconciled', async () => {
    const resources = new MiniAppResources(AUTH, 'home-pause-ambiguous')
    resources.publishConfirmed('dashboard', dashboard({ copy_state: 'active' }))
    resources.publishConfirmed('positions', positionPage({ positions: [], count: 0 }))
    const reconcile = deferred<DashboardDTO>()
    const dashboardSpy = vi.spyOn(api, 'dashboard').mockReturnValue(reconcile.promise)
    const pauseSpy = vi
      .spyOn(api, 'pause')
      .mockRejectedValue(new ApiError(0, 'timeout_error', 'response lost'))

    const first = render(
      <HomeScreen auth={AUTH} resources={resources} onNotConnected={noop} />,
    )
    fireEvent.click(screen.getByRole('button', { name: /поставить на паузу/i }))
    await waitFor(() => expect(dashboardSpy).toHaveBeenCalledTimes(1))
    expect(pauseSpy).toHaveBeenCalledTimes(1)
    expect(resources.mutationLocked('pause-resume')).toBe(true)

    first.unmount()
    render(<HomeScreen auth={AUTH} resources={resources} onNotConnected={noop} />)
    const reopened = screen.getByRole('button', { name: /поставить на паузу/i })
    expect(reopened).toBeDisabled()
    fireEvent.click(reopened)
    expect(pauseSpy).toHaveBeenCalledTimes(1)

    reconcile.resolve(dashboard({ copy_state: 'paused' }))
    await screen.findByRole('button', { name: /возобновить/i })
    await waitFor(() => expect(resources.mutationLocked('pause-resume')).toBe(false))
    expect(pauseSpy).toHaveBeenCalledTimes(1)
  })

  it('ambiguous pause never treats a pre-mutation strong Dashboard flight as reconciliation', async () => {
    const resources = new MiniAppResources(AUTH, 'home-pause-causal-fence')
    resources.publishConfirmed('dashboard', dashboard({ copy_state: 'active' }))
    resources.publishConfirmed('positions', positionPage({ positions: [], count: 0 }))
    const oldGate = deferred<DashboardDTO>()
    const currentGate = deferred<DashboardDTO>()
    const dashboardSpy = vi
      .spyOn(api, 'dashboard')
      .mockReturnValueOnce(oldGate.promise)
      .mockReturnValueOnce(currentGate.promise)
    const pauseSpy = vi
      .spyOn(api, 'pause')
      .mockRejectedValue(new ApiError(0, 'timeout_error', 'response lost'))

    // Simulate a TTL/foreground strong read that was admitted before the
    // mutation. Home is still allowed to act from its cached confirmed DTO.
    const oldRead = resources.refresh('dashboard', 'strong')
    render(<HomeScreen auth={AUTH} resources={resources} onNotConnected={noop} />)
    fireEvent.click(screen.getByRole('button', { name: /поставить на паузу/i }))
    await waitFor(() => expect(pauseSpy).toHaveBeenCalledTimes(1))

    // Old pre-write evidence says "active". It must be discarded and cause a
    // fresh post-boundary strong read rather than unlocking the mutation.
    oldGate.resolve(dashboard({ copy_state: 'active' }))
    await oldRead
    await waitFor(() => expect(dashboardSpy).toHaveBeenCalledTimes(2))
    expect(resources.mutationState('pause-resume')).toBe('active')
    expect(screen.getByRole('button', { name: /пауз/i })).toBeDisabled()

    currentGate.resolve(dashboard({ copy_state: 'paused' }))
    await screen.findByRole('button', { name: /возобновить/i })
    await waitFor(() => expect(resources.mutationState('pause-resume')).toBeNull())
    expect(pauseSpy).toHaveBeenCalledTimes(1)
  })

  it('keeps pause fail-closed across remount, then a later strong read unlocks exactly one fresh user retry', async () => {
    const resources = new MiniAppResources(AUTH, 'home-pause-double-unknown')
    const onMutated = vi.fn()
    resources.publishConfirmed('dashboard', dashboard({ copy_state: 'active' }))
    resources.publishConfirmed('positions', positionPage({ positions: [], count: 0 }))
    const dashboardSpy = vi
      .spyOn(api, 'dashboard')
      .mockRejectedValueOnce(new ApiError(0, 'network_error', 'verify lost'))
      .mockResolvedValueOnce(dashboard({ copy_state: 'active' }))
    const pauseSpy = vi
      .spyOn(api, 'pause')
      .mockRejectedValueOnce(new ApiError(0, 'timeout_error', 'response lost'))
      .mockResolvedValueOnce(dashboard({ copy_state: 'paused' }))

    const first = render(
      <HomeScreen auth={AUTH} resources={resources} onNotConnected={noop} onMutated={onMutated} />,
    )
    fireEvent.click(screen.getByRole('button', { name: /поставить на паузу/i }))
    await waitFor(() => expect(dashboardSpy).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(resources.mutationLocked('pause-resume')).toBe(true))
    expect(pauseSpy).toHaveBeenCalledTimes(1)

    first.unmount()
    render(
      <HomeScreen auth={AUTH} resources={resources} onNotConnected={noop} onMutated={onMutated} />,
    )
    const reopened = screen.getByRole('button', { name: /поставить на паузу/i })
    expect(reopened).toBeDisabled()
    fireEvent.click(reopened)
    expect(pauseSpy).toHaveBeenCalledTimes(1)
    expect(resources.mutationLocked('pause-resume')).toBe(true)
    expect(resources.mutationState('pause-resume')).toBe('unknown')

    // Network later recovers. Unknown keeps pause blocked, but explicit
    // authoritative recovery remains available in the UI. Active in-flight
    // mutations still disable that button; only retained uncertainty may use it.
    const recover = screen.getByRole('button', { name: 'Обновить данные' })
    expect(recover).toBeEnabled()
    fireEvent.click(recover)
    await waitFor(() => expect(resources.mutationState('pause-resume')).toBeNull())
    expect(dashboardSpy).toHaveBeenCalledTimes(2)
    expect(pauseSpy).toHaveBeenCalledTimes(1)
    expect(onMutated).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: /поставить на паузу/i })).toBeEnabled()

    // A new user gesture may now admit one fresh mutation; double-click cannot
    // turn recovery into a replay loop.
    const retry = screen.getByRole('button', { name: /поставить на паузу/i })
    fireEvent.click(retry)
    fireEvent.click(retry)
    await screen.findByRole('button', { name: /возобновить/i })
    expect(pauseSpy).toHaveBeenCalledTimes(2)
  })

  it('qualifies cached Home and Positions as transport-stale while refresh is pending', async () => {
    const resources = new MiniAppResources(AUTH, 'root-stale-honesty')
    resources.publishConfirmed('dashboard', dashboard({ system_state: 'ready' }))
    resources.publishConfirmed('positions', positionPage({ positions: [], count: 0 }))
    resources.invalidate(['dashboard', 'positions'])
    vi.spyOn(api, 'dashboard').mockReturnValue(new Promise(() => {}))
    vi.spyOn(api, 'positions').mockReturnValue(new Promise(() => {}))

    const home = render(
      <HomeScreen auth={AUTH} resources={resources} onNotConnected={noop} active={true} />,
    )
    expect(screen.getByText(/данные требуют проверки/i)).toBeDefined()
    home.unmount()

    render(<PositionsScreen auth={AUTH} resources={resources} active={true} warm={true} />)
    expect(screen.getByText(/снимок позиций устарел/i)).toBeDefined()
  })

  it('reconciles an ambiguous resume timeout from authoritative Dashboard without replaying resume', async () => {
    const resources = new MiniAppResources(AUTH, 'home-resume-ambiguous')
    resources.publishConfirmed('dashboard', dashboard({ copy_state: 'paused' }))
    resources.publishConfirmed('positions', positionPage({ positions: [], count: 0 }))
    vi.spyOn(api, 'beginConfirmation').mockResolvedValue({
      token: 'resume-ambiguous',
      action: 'resume',
      account_id: 'copyc1',
      expires_at: null,
    })
    const resumeSpy = vi
      .spyOn(api, 'resume')
      .mockRejectedValue(new ApiError(0, 'network_error', 'response lost'))
    const dashboardSpy = vi
      .spyOn(api, 'dashboard')
      .mockResolvedValue(dashboard({ copy_state: 'active' }))

    render(<HomeScreen auth={AUTH} resources={resources} onNotConnected={noop} />)
    fireEvent.click(screen.getByRole('button', { name: /возобновить/i }))
    await screen.findByText('Возобновить копирование?')
    fireEvent.click(screen.getByRole('button', { name: 'Подтвердить возобновление' }))

    await screen.findByRole('button', { name: /поставить на паузу/i })
    expect(resumeSpy).toHaveBeenCalledTimes(1)
    expect(dashboardSpy).toHaveBeenCalledTimes(1)
    expect(resources.mutationLocked('pause-resume')).toBe(false)
  })

  it('ambiguous resume fences a pre-mutation strong Dashboard flight before recovery', async () => {
    const resources = new MiniAppResources(AUTH, 'home-resume-causal-fence')
    resources.publishConfirmed('dashboard', dashboard({ copy_state: 'paused' }))
    resources.publishConfirmed('positions', positionPage({ positions: [], count: 0 }))
    const oldGate = deferred<DashboardDTO>()
    const currentGate = deferred<DashboardDTO>()
    const dashboardSpy = vi
      .spyOn(api, 'dashboard')
      .mockReturnValueOnce(oldGate.promise)
      .mockReturnValueOnce(currentGate.promise)
    vi.spyOn(api, 'beginConfirmation').mockResolvedValue({
      token: 'resume-causal-fence',
      action: 'resume',
      account_id: 'copyc1',
      expires_at: null,
    })
    const resumeSpy = vi
      .spyOn(api, 'resume')
      .mockRejectedValue(new ApiError(0, 'network_error', 'response lost'))

    const oldRead = resources.refresh('dashboard', 'strong')
    render(<HomeScreen auth={AUTH} resources={resources} onNotConnected={noop} />)
    fireEvent.click(screen.getByRole('button', { name: /возобновить/i }))
    await screen.findByText('Возобновить копирование?')
    fireEvent.click(screen.getByRole('button', { name: 'Подтвердить возобновление' }))
    await waitFor(() => expect(resumeSpy).toHaveBeenCalledTimes(1))

    oldGate.resolve(dashboard({ copy_state: 'paused' }))
    await oldRead
    await waitFor(() => expect(dashboardSpy).toHaveBeenCalledTimes(2))
    expect(resources.mutationState('pause-resume')).toBe('active')

    currentGate.resolve(dashboard({ copy_state: 'active' }))
    await screen.findByRole('button', { name: /поставить на паузу/i })
    await waitFor(() => expect(resources.mutationState('pause-resume')).toBeNull())
    expect(resumeSpy).toHaveBeenCalledTimes(1)
  })

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
    await screen.findByRole('heading', { name: 'PnL открытых позиций' })
    expect(screen.queryByLabelText('Загрузка состояния')).toBeNull()
    expect(screen.queryByLabelText('Загрузка позиций')).toBeNull()
    expect(positionsSpy).not.toHaveBeenCalled()

    positionsGate.resolve(positionPage())
    expect(screen.queryByText('Активная позиция')).toBeNull()
    expect(screen.queryByLabelText('Загрузка позиций')).toBeNull()
    // The partial-ready flip must not spawn its own background refresh loop.
    await act(async () => {})
    expect(dashboardSpy).toHaveBeenCalledTimes(1)
    expect(positionsSpy).not.toHaveBeenCalled()
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
    await screen.findByRole('heading', { name: 'PnL открытых позиций' })
    expect(dashboardSpy).toHaveBeenCalledTimes(1)

    // Fresh data: reactivation is free.
    rerender(<HomeScreen auth={AUTH} onNotConnected={noop} active={true} />)
    await screen.findByRole('heading', { name: 'PnL открытых позиций' })
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

  it('full startup budget is four reads total: session + shared dashboard + positions + activity', async () => {
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
    const activitySpy = vi.spyOn(api, 'activityFeed').mockResolvedValue(activityPage())
    render(<App />)

    await screen.findByText('Настройки копирования')
    await screen.findByText('Обновить события')
    expect(sessionSpy).toHaveBeenCalledTimes(1)
    expect(dashboardSpy).toHaveBeenCalledTimes(1)
    expect(positionsSpy).toHaveBeenCalledTimes(1)
    expect(activitySpy).toHaveBeenCalledTimes(1)
    expect(
      sessionSpy.mock.calls.length +
        dashboardSpy.mock.calls.length +
        positionsSpy.mock.calls.length +
        activitySpy.mock.calls.length,
    ).toBe(4)
  })

  it('a resource-level invalid session disposes the connected scope and returns App to the gate', async () => {
    mockTelegramSession()
    vi.spyOn(api, 'session').mockResolvedValue({
      user_id: 777,
      access_state: 'approved',
      environment: 'testnet',
      connection_state: 'connected',
      account_connected: true,
      account_masked: '0xABC…DEF',
    })
    const dashboardSpy = vi
      .spyOn(api, 'dashboard')
      .mockRejectedValue(new ApiError(401, 'invalid_session', 'expired'))
    const positionsSpy = vi.spyOn(api, 'positions')
    const activitySpy = vi.spyOn(api, 'activityFeed')

    render(<App />)
    await screen.findByRole('heading', { name: 'Откройте из бота' })
    expect(dashboardSpy).toHaveBeenCalledTimes(1)
    expect(positionsSpy).not.toHaveBeenCalled()
    expect(activitySpy).not.toHaveBeenCalled()
  })

  it('a direct Home mutation 401 uses the same hard scope teardown and returns App to the gate', async () => {
    mockTelegramSession()
    vi.spyOn(api, 'session').mockResolvedValue({
      user_id: 777,
      access_state: 'approved',
      environment: 'testnet',
      connection_state: 'connected',
      account_connected: true,
      account_masked: '0xABC…DEF',
    })
    vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard({ copy_state: 'active' }))
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    vi.spyOn(api, 'activityFeed').mockResolvedValue(activityPage({ items: [], has_next: false }))
    const pauseSpy = vi
      .spyOn(api, 'pause')
      .mockRejectedValue(new ApiError(401, 'invalid_session', 'expired'))

    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: /поставить на паузу/i }))
    await screen.findByRole('heading', { name: 'Откройте из бота' })
    expect(pauseSpy).toHaveBeenCalledTimes(1)
  })

  it('warmup waits until Home reaches ready', async () => {
    const dashboardGate = deferred<DashboardDTO>()
    const positionsGate = deferred<PositionPageDTO>()
    const dashboardSpy = vi.spyOn(api, 'dashboard').mockReturnValue(dashboardGate.promise)
    const positionsSpy = vi.spyOn(api, 'positions').mockReturnValue(positionsGate.promise)
    const activitySpy = vi
      .spyOn(api, 'activityFeed')
      .mockResolvedValue(activityPage({ items: [], has_next: false }))
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    await act(async () => {})
    // Home still loading: no snapshot yet, hidden tabs stay quiet.
    expect(dashboardSpy).toHaveBeenCalledTimes(1)
    expect(positionsSpy).toHaveBeenCalledTimes(0)
    expect(activitySpy).toHaveBeenCalledTimes(0)

    dashboardGate.resolve(dashboard())
    await screen.findByRole('heading', { name: 'PnL открытых позиций' })
    positionsGate.resolve(positionPage({ positions: [], count: 0 }))
    await screen.findByText('Настройки копирования')
    expect(dashboardSpy).toHaveBeenCalledTimes(1)
    expect(positionsSpy).toHaveBeenCalledTimes(1)
    expect(activitySpy).toHaveBeenCalledTimes(1)
  })

  it('an early Positions tap gets exactly one correctness follow-up after Home strong refresh', async () => {
    const dashboardGate = deferred<DashboardDTO>()
    vi.spyOn(api, 'dashboard').mockReturnValue(dashboardGate.promise)
    const positionsSpy = vi
      .spyOn(api, 'positions')
      .mockResolvedValueOnce(positionPage({ positions: [], count: 0 }))
      .mockResolvedValueOnce(positionPage({ positions: [position({ symbol: 'ETHUSDT' })], count: 1 }))
    vi.spyOn(api, 'activityFeed').mockResolvedValue(activityPage({ items: [], has_next: false }))
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    // Home still loading, user taps Positions: cold path is immediate and can
    // only see the server cache that predates Home's strong refresh.
    fireEvent.click(tabButton('Позиции'))
    await screen.findByText('Нет открытых позиций')
    expect(positionsSpy).toHaveBeenCalledTimes(1)

    dashboardGate.resolve(dashboard())
    await screen.findByText('Настройки копирования')
    // A successful strong dashboard defines a newer server snapshot. The
    // already-settled weak positions DTO is therefore invalidated and the
    // active Positions consumer performs exactly one correctness follow-up.
    await screen.findByText('Открытые 1')
    // Both the active Positions root and hidden Home preview consume the same
    // refreshed DTO; this is sharing, not an extra network read.
    expect(screen.getAllByText(/ETHUSDT/)).toHaveLength(1)
    expect(positionsSpy).toHaveBeenCalledTimes(2)
    expect(positionsSpy.mock.lastCall?.slice(0, 2)).toEqual([AUTH, false])

    // Home's later preview/warmup shares that refreshed DTO: no third POST.
    await act(async () => {})
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
      .spyOn(api, 'activityFeed')
      .mockResolvedValue(activityPage({ items: [], has_next: false }))
    const { rerender } = render(<ActivityScreen auth={AUTH} />)
    await screen.findByText('Обновить события')
    expect(activitySpy).toHaveBeenCalledTimes(1)

    const bounce = async (ageMs: number) => {
      rerender(<ActivityScreen auth={AUTH} active={false} />)
      const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(t0 + ageMs)
      rerender(<ActivityScreen auth={AUTH} active={true} />)
      nowSpy.mockRestore()
      await screen.findByText('Обновить события')
    }
    await bounce(35_000)
    expect(activitySpy).toHaveBeenCalledTimes(1)
    await bounce(65_000)
    expect(activitySpy).toHaveBeenCalledTimes(2)
  })

  it('Activity ignores foreground while hidden, then TTL-refreshes on activation and active foreground', async () => {
    const t0 = 1_800_000_000_000
    const now = vi.spyOn(Date, 'now').mockReturnValue(t0)
    vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    const activitySpy = vi
      .spyOn(api, 'activityFeed')
      .mockResolvedValue(activityPage({ items: [], has_next: false }))

    render(<AppShell auth={AUTH} onNotConnected={noop} />)
    await screen.findByText('Обновить события')
    expect(activitySpy).toHaveBeenCalledTimes(1)

    // Activity is still a hidden keep-alive root. Foreground must not make it
    // poll even though its cached page is now older than the 60 s floor.
    now.mockReturnValue(t0 + ACTIVITY_REVALIDATE_MS + 5_000)
    act(() => window.dispatchEvent(new Event('focus')))
    await act(async () => {})
    expect(activitySpy).toHaveBeenCalledTimes(1)

    // Showing the aged tab is a legitimate refresh edge.
    fireEvent.click(tabButton('Активность'))
    await act(async () => {})
    expect(activitySpy).toHaveBeenCalledTimes(2)

    // Once Activity is active, a later app foreground also re-checks TTL.
    now.mockReturnValue(t0 + (ACTIVITY_REVALIDATE_MS + 5_000) * 2)
    act(() => window.dispatchEvent(new Event('focus')))
    await act(async () => {})
    expect(activitySpy).toHaveBeenCalledTimes(3)
    now.mockRestore()
  })

  it('revalidation past TTL never resets appended pagination', async () => {
    const t0 = Date.now()
    const activitySpy = vi
      .spyOn(api, 'activityFeed')
      .mockResolvedValueOnce(
        activityPage({ items: [activityItem()], has_next: true }),
      )
      .mockResolvedValueOnce(
        activityPage({ items: [activityItem({ title: 'Сделка B открыта' })], page: 1, has_next: false }),
      )
      .mockResolvedValue({ ...activityPage(), latest_seq: 11 })
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
    // A freshness probe runs, but never replaces the pinned paginated slice.
    expect(screen.getByText('Открыта позиция BTCUSDT')).toBeDefined()
    expect(await screen.findByText('Есть новые события')).toBeDefined()
    expect(activitySpy).toHaveBeenCalledTimes(3)
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

  it('Risk save shares Dashboard immediately, then Settings performs only its required stale follow-up', async () => {
    const back = installBackButton()
    const bgGate = deferred<DashboardDTO>()
    const dashboardSpy = vi
      .spyOn(api, 'dashboard')
      .mockResolvedValueOnce(dashboard({ risk_percent: '0.01' }))
      .mockReturnValue(bgGate.promise)
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    vi.spyOn(api, 'activityFeed').mockResolvedValue(activityPage({ items: [], has_next: false }))
    vi.spyOn(api, 'risk').mockResolvedValue(riskDto())
    const setter = vi
      .spyOn(api, 'setRiskPercent')
      .mockResolvedValue(riskDto({ risk_percent: '0.02' }))
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')
    expect(dashboardSpy).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: /риск на сделку.*1%/i })).toBeDefined()

    fireEvent.click(screen.getByRole('button', { name: /риск на сделку/i }))
    await screen.findByRole('button', { name: /сохранить/i })
    fireEvent.click(screen.getByRole('radio', { name: '2,0%' }))
    fireEvent.click(screen.getByRole('button', { name: /сохранить 2,0%/i }))
    await screen.findByText(/сохранено · 2,0%/i)
    expect(setter).toHaveBeenCalledTimes(1)

    back.press()
    const restoredHeading = await screen.findByRole('heading', { name: 'Настройки' })
    expect(isHidden(restoredHeading)).toBe(false)
    // Old shared DTO paints instantly; exactly one stale follow-up hangs on
    // the gate instead of a screen-local duplicate read.
    await act(async () => {})
    expect(screen.queryByLabelText('Загрузка настроек')).toBeNull()
    expect(dashboardSpy).toHaveBeenCalledTimes(2)
    expect(dashboardSpy.mock.lastCall?.slice(0, 2)).toEqual([AUTH, false])
    expect(screen.getByRole('button', { name: /риск на сделку.*1%/i })).toBeDefined()

    bgGate.resolve(dashboard({ risk_percent: '0.02' }))
    await screen.findByRole('button', { name: /риск на сделку.*2%/i })
    // Settles quiet: invalidation fires once, never loops.
    await act(async () => {})
    await act(async () => {})
    expect(dashboardSpy).toHaveBeenCalledTimes(2)
  })

  it('Notifications save shares Dashboard immediately, then Settings performs one stale follow-up', async () => {
    const back = installBackButton()
    const bgGate = deferred<DashboardDTO>()
    const dashboardSpy = vi
      .spyOn(api, 'dashboard')
      .mockResolvedValueOnce(dashboard())
      .mockReturnValue(bgGate.promise)
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    vi.spyOn(api, 'activityFeed').mockResolvedValue(activityPage({ items: [], has_next: false }))
    vi.spyOn(api, 'notifications').mockResolvedValue(notificationsDto())
    const saver = vi
      .spyOn(api, 'setNotifications')
      .mockResolvedValue(notificationsDto({ enabled: false }))
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')
    expect(dashboardSpy).toHaveBeenCalledTimes(1)

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
    expect(dashboardSpy).toHaveBeenCalledTimes(2)
    bgGate.resolve(dashboard())
    await act(async () => {})
    expect(dashboardSpy).toHaveBeenCalledTimes(2)
  })

  it('Home pause publishes the shared Dashboard immediately while Activity still invalidates', async () => {
    const dashboardSpy = vi
      .spyOn(api, 'dashboard')
      .mockResolvedValue(dashboard({ copy_state: 'active' }))
    const activitySpy = vi
      .spyOn(api, 'activityFeed')
      .mockResolvedValue(activityPage({ items: [], has_next: false }))
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    const pauser = vi
      .spyOn(api, 'pause')
      .mockResolvedValue(dashboard({ copy_state: 'paused' }))
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    fireEvent.click(await screen.findByRole('button', { name: /поставить на паузу/i }))
    await screen.findByRole('button', { name: /возобновить/i })
    expect(pauser).toHaveBeenCalledTimes(1)

    // The confirmed pause response is the shared Dashboard. Settings should
    // paint it immediately without another read or a skeleton.
    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')
    await act(async () => {})
    expect(screen.queryByLabelText('Загрузка настроек')).toBeNull()
    expect(screen.getByText('Новые входы на паузе')).toBeDefined()
    expect(dashboardSpy).toHaveBeenCalledTimes(1)

    fireEvent.click(tabButton('Активность'))
    await screen.findByText('Обновить события')
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
    await screen.findByRole('heading', { name: 'PnL открытых позиций' })
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

    fireEvent.click(screen.getByRole('button', { name: /риск на сделку/i }))
    await screen.findByRole('button', { name: /сохранить/i })
    expect(scroller!.scrollTop).toBe(0)

    scroller!.scrollTop = 150
    back.press()
    const restoredHeading = await screen.findByRole('heading', { name: 'Настройки' })
    expect(isHidden(restoredHeading)).toBe(false)
    expect(scroller!.scrollTop).toBe(300)
  })

  it('reopening the same nested route gets a fresh scroll slot and starts at the top', async () => {
    const back = installBackButton()
    vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    vi.spyOn(api, 'risk').mockResolvedValue(riskDto())
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')
    const scroller = document.querySelector('.shell-content') as HTMLElement

    fireEvent.click(screen.getByRole('button', { name: /риск на сделку/i }))
    await screen.findByRole('button', { name: /сохранить/i })
    scroller.scrollTop = 180
    back.press()
    await screen.findByRole('heading', { name: 'Настройки' })

    fireEvent.click(screen.getByRole('button', { name: /риск на сделку/i }))
    await screen.findByRole('button', { name: /сохранить/i })
    expect(scroller.scrollTop).toBe(0)
  })

  it('Back restores focus to the Settings row that opened the nested screen', async () => {
    const back = installBackButton()
    vi.spyOn(api, 'dashboard').mockResolvedValue(dashboard())
    vi.spyOn(api, 'positions').mockResolvedValue(positionPage({ positions: [], count: 0 }))
    vi.spyOn(api, 'risk').mockResolvedValue(riskDto())
    render(<AppShell auth={AUTH} onNotConnected={noop} />)

    fireEvent.click(tabButton('Настройки'))
    await screen.findByText('Настройки копирования')
    const opener = screen.getByRole('button', { name: /риск на сделку/i })
    opener.focus()
    expect(opener).toHaveFocus()

    fireEvent.click(opener)
    await screen.findByRole('button', { name: /сохранить/i })
    back.press()
    await screen.findByRole('heading', { name: 'Настройки' })
    await waitFor(() => expect(opener).toHaveFocus())
  })
})

describe('Corrective: resource invalidation retry', () => {
  function settings(resources: MiniAppResources, active: boolean) {
    return (
      <SettingsScreen
        auth={AUTH}
        resources={resources}
        onOpen={noop}
        onNotConnected={noop}
        active={active}
        warm={true}
      />
    )
  }

  it('failed invalidation refresh stays stale and waits for reactivation before one retry', async () => {
    const resources = new MiniAppResources(AUTH, 'settings-retry')
    const dashboardSpy = vi
      .spyOn(api, 'dashboard')
      .mockResolvedValueOnce(dashboard())
      .mockRejectedValueOnce(new ApiError(500, 'network_error', 'down'))
      .mockResolvedValue(dashboard())
    const { rerender } = render(settings(resources, true))
    await screen.findByText('Настройки копирования')
    expect(dashboardSpy).toHaveBeenCalledTimes(1)

    act(() => resources.invalidate(['dashboard']))
    await act(async () => {})
    expect(dashboardSpy).toHaveBeenCalledTimes(2)
    // Stale UI stays visible and a failed background refresh must not create
    // an immediate retry loop while the activation edge is unchanged.
    expect(screen.queryByLabelText('Загрузка настроек')).toBeNull()
    expect(screen.getByText('Настройки копирования')).toBeDefined()
    await act(async () => {})
    expect(dashboardSpy).toHaveBeenCalledTimes(2)

    // A real foreground edge retries once, then returns to quiet.
    rerender(settings(resources, false))
    await act(async () => {})
    rerender(settings(resources, true))
    await act(async () => {})
    expect(dashboardSpy).toHaveBeenCalledTimes(3)
    rerender(settings(resources, false))
    await act(async () => {})
    rerender(settings(resources, true))
    await act(async () => {})
    expect(dashboardSpy).toHaveBeenCalledTimes(3)
  })
})
