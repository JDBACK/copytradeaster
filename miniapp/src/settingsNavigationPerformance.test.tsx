// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api, ApiError } from './api'
import { NestedRoute } from './components/AppShell'
import type { NestedScreen } from './navigation'
import { MiniAppResources } from './resources'
import { AccountBuilderScreen } from './screens/AccountBuilderScreen'
import { SettingsScreen } from './screens/SettingsScreen'
import { SystemStatusScreen } from './screens/SystemStatusScreen'
import type {
  AccountBuilderDTO,
  DashboardDTO,
  NotificationSettingsDTO,
  RiskDTO,
  StatusDTO,
} from './types'

const AUTH = { initData: 'settings-performance-test' }
const noop = () => {}

// jsdom gaps the production code never meets in Telegram WebView.
if ((globalThis as { CSS?: unknown }).CSS === undefined) {
  ;(globalThis as { CSS?: unknown }).CSS = { escape: (value: string) => value }
}
if (typeof Element !== 'undefined' && typeof Element.prototype.scrollTo !== 'function') {
  Element.prototype.scrollTo = () => {}
}

function dashboard(overrides: Partial<DashboardDTO> = {}): DashboardDTO {
  return {
    copy_state: 'paused',
    system_state: 'ready',
    account_masked: '0xAAAA…BBBB',
    signer_masked: '0x1111…2222',
    risk_percent: '0.01',
    open_positions: 1,
    notification_enabled: true,
    is_admin: false,
    snapshot_captured_at: '2026-09-07T09:00:00+00:00',
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

function notificationsDto(
  overrides: Partial<NotificationSettingsDTO> = {},
): NotificationSettingsDTO {
  return {
    enabled: true,
    last_delivery_at: null,
    pending: 0,
    failed: 0,
    ...overrides,
  }
}

function statusDto(overrides: Partial<StatusDTO> = {}): StatusDTO {
  return {
    state: 'ready',
    system_state: 'ready',
    copy_state: 'paused',
    aster_state: 'ok',
    permissions_state: 'valid',
    account_masked: '0xAAAA…BBBB',
    signer_masked: '0x1111…2222',
    positions_count: 1,
    permissions: { can_read: true, can_perp_trade: true, can_withdraw: false },
    snapshot_captured_at: '2026-09-07T09:00:00+00:00',
    last_check_at: '2026-09-07T09:00:00+00:00',
    age_seconds: 5,
    reason: 'none',
    remediation: null,
    ...overrides,
  }
}

function accountBuilderDto(overrides: Partial<AccountBuilderDTO> = {}): AccountBuilderDTO {
  return {
    identity_source: 'builder',
    state: 'verified',
    reason: 'none',
    remediation: null,
    account_masked: '0xAAAA…BBBB',
    signer_masked: '0x1111…2222',
    builder_address_masked: '0x2576…6EF8',
    builder_name: 'copytradeaster',
    max_fee_rate: '0.0001',
    permission: { can_read: true, can_perp_trade: true, can_withdraw: false },
    agent_expires_at_ms: 2_000_000_000_000,
    verified_at: '2026-09-07T09:00:00+00:00',
    last_check_at: '2026-09-07T09:00:00+00:00',
    age_seconds: 5,
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

function SettingsHarness({ resources }: { resources: MiniAppResources }) {
  const [nested, setNested] = useState<NestedScreen | null>(null)
  return (
    <>
      <div hidden={nested !== null}>
        <SettingsScreen
          auth={AUTH}
          resources={resources}
          accountMasked="0xAAAA…BBBB"
          onOpen={setNested}
          onNotConnected={noop}
          active={nested === null}
          warm
        />
      </div>
      {nested && (
        <>
          <NestedRoute
            nested={nested}
            auth={AUTH}
            resources={resources}
            accountMasked="0xAAAA…BBBB"
            onNotConnected={noop}
          />
          <button type="button" onClick={() => setNested(null)}>
            Test back
          </button>
        </>
      )}
    </>
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('Settings G2 navigation performance', () => {
  it('hidden warm Settings triggers zero nested speculative reads', async () => {
    const resources = new MiniAppResources(AUTH, 'hidden-settings')
    resources.publishConfirmed('dashboard', dashboard())
    const risk = vi.spyOn(api, 'risk').mockResolvedValue(riskDto())
    const notifications = vi.spyOn(api, 'notifications').mockResolvedValue(notificationsDto())
    const status = vi.spyOn(api, 'status').mockResolvedValue(statusDto())
    const accountBuilder = vi.spyOn(api, 'accountBuilder').mockResolvedValue(accountBuilderDto())

    render(
      <SettingsScreen
        auth={AUTH}
        resources={resources}
        onOpen={noop}
        onNotConnected={noop}
        active={false}
        warm
      />,
    )
    await screen.findByText('Настройки копирования')
    await act(async () => {})

    expect(risk).not.toHaveBeenCalled()
    expect(notifications).not.toHaveBeenCalled()
    expect(status).not.toHaveBeenCalled()
    expect(accountBuilder).not.toHaveBeenCalled()
  })

  it('first real Settings activation starts at most two speculative reads and keeps every prefetch weak', async () => {
    const resources = new MiniAppResources(AUTH, 'active-settings')
    resources.publishConfirmed('dashboard', dashboard())
    const riskGate = deferred<RiskDTO>()
    const notificationsGate = deferred<NotificationSettingsDTO>()
    const statusGate = deferred<StatusDTO>()
    const accountGate = deferred<AccountBuilderDTO>()
    const risk = vi.spyOn(api, 'risk').mockReturnValue(riskGate.promise)
    const notifications = vi.spyOn(api, 'notifications').mockReturnValue(notificationsGate.promise)
    const status = vi.spyOn(api, 'status').mockReturnValue(statusGate.promise)
    const accountBuilder = vi.spyOn(api, 'accountBuilder').mockReturnValue(accountGate.promise)

    const { rerender } = render(
      <SettingsScreen
        auth={AUTH}
        resources={resources}
        onOpen={noop}
        onNotConnected={noop}
        active={false}
        warm
      />,
    )
    await screen.findByText('Настройки копирования')

    rerender(
      <SettingsScreen
        auth={AUTH}
        resources={resources}
        onOpen={noop}
        onNotConnected={noop}
        active
        warm
      />,
    )
    await waitFor(() => {
      expect(risk).toHaveBeenCalledTimes(1)
      expect(notifications).toHaveBeenCalledTimes(1)
    })
    expect(status).not.toHaveBeenCalled()
    expect(accountBuilder).not.toHaveBeenCalled()

    await act(async () => {
      riskGate.resolve(riskDto())
      notificationsGate.resolve(notificationsDto())
      await Promise.resolve()
    })
    await waitFor(() => {
      expect(status).toHaveBeenCalledTimes(1)
      expect(accountBuilder).toHaveBeenCalledTimes(1)
    })

    expect(status).toHaveBeenLastCalledWith(AUTH, false, expect.any(Object))
    expect(accountBuilder).toHaveBeenLastCalledWith(AUTH, false, expect.any(Object))
    expect(status.mock.calls.every((call) => call[1] === false)).toBe(true)
    expect(accountBuilder.mock.calls.every((call) => call[1] === false)).toBe(true)

    statusGate.resolve(statusDto())
    accountGate.resolve(accountBuilderDto())
  })

  it('an early nested click joins or prioritizes the target without a duplicate read', async () => {
    const resources = new MiniAppResources(AUTH, 'early-click')
    resources.publishConfirmed('dashboard', dashboard())
    const riskGate = deferred<RiskDTO>()
    const notificationsGate = deferred<NotificationSettingsDTO>()
    const accountGate = deferred<AccountBuilderDTO>()
    const risk = vi.spyOn(api, 'risk').mockReturnValue(riskGate.promise)
    vi.spyOn(api, 'notifications').mockReturnValue(notificationsGate.promise)
    vi.spyOn(api, 'status').mockResolvedValue(statusDto())
    const accountBuilder = vi.spyOn(api, 'accountBuilder').mockReturnValue(accountGate.promise)

    render(<SettingsHarness resources={resources} />)
    await waitFor(() => expect(risk).toHaveBeenCalledTimes(1))
    expect(accountBuilder).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: /Подключение Aster/ }))
    await waitFor(() => expect(accountBuilder).toHaveBeenCalledTimes(1))
    expect(accountBuilder).toHaveBeenLastCalledWith(AUTH, false, expect.any(Object))
    expect(screen.getByLabelText('Загрузка аккаунта')).toBeInTheDocument()

    accountGate.resolve(accountBuilderDto())
    await screen.findByText('Builder подтверждён')
    expect(accountBuilder).toHaveBeenCalledTimes(1)

    riskGate.resolve(riskDto())
    notificationsGate.resolve(notificationsDto())
  })

  it('cached Risk and Notifications revisits inside TTL paint immediately with no new read or skeleton', async () => {
    const resources = new MiniAppResources(AUTH, 'cached-nested')
    resources.publishConfirmed('dashboard', dashboard())
    const risk = vi.spyOn(api, 'risk').mockResolvedValue(riskDto())
    const notifications = vi.spyOn(api, 'notifications').mockResolvedValue(notificationsDto())
    vi.spyOn(api, 'status').mockResolvedValue(statusDto())
    vi.spyOn(api, 'accountBuilder').mockResolvedValue(accountBuilderDto())

    await resources.ensure('risk', { strength: 'weak' })
    await resources.ensure('notifications', { strength: 'weak' })
    expect(risk).toHaveBeenCalledTimes(1)
    expect(notifications).toHaveBeenCalledTimes(1)

    render(<SettingsHarness resources={resources} />)
    await screen.findByText('Настройки копирования')

    fireEvent.click(screen.getByRole('button', { name: /риск на сделку/i }))
    expect(screen.getByText('Риск на Stop', { selector: 'h1' })).toBeInTheDocument()
    expect(screen.queryByLabelText('Загрузка настроек риска')).toBeNull()
    expect(risk).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Test back' }))

    fireEvent.click(screen.getByRole('button', { name: /уведомления/i }))
    expect(screen.getByText('Уведомления', { selector: 'h1' })).toBeInTheDocument()
    expect(screen.queryByLabelText('Загрузка настроек уведомлений')).toBeNull()
    expect(notifications).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Test back' }))

    fireEvent.click(screen.getByRole('button', { name: /риск на сделку/i }))
    expect(screen.queryByLabelText('Загрузка настроек риска')).toBeNull()
    expect(risk).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Test back' }))
    fireEvent.click(screen.getByRole('button', { name: /уведомления/i }))
    expect(screen.queryByLabelText('Загрузка настроек уведомлений')).toBeNull()
    expect(notifications).toHaveBeenCalledTimes(1)
  })

  it('cached Settings stays visible but never shows fresh green status while Dashboard refresh is stale or failed', async () => {
    const resources = new MiniAppResources(AUTH, 'settings-stale-truth')
    resources.publishConfirmed('dashboard', dashboard())
    vi.spyOn(api, 'risk').mockResolvedValue(riskDto())
    vi.spyOn(api, 'notifications').mockResolvedValue(notificationsDto())
    vi.spyOn(api, 'status').mockResolvedValue(statusDto())
    vi.spyOn(api, 'accountBuilder').mockResolvedValue(accountBuilderDto())
    const refreshGate = deferred<DashboardDTO>()
    const dashboardRead = vi.spyOn(api, 'dashboard').mockReturnValue(refreshGate.promise)

    render(
      <SettingsScreen
        auth={AUTH}
        resources={resources}
        onOpen={noop}
        onNotConnected={noop}
        active
        warm
      />,
    )
    expect(screen.getByText('Всё работает')).toBeInTheDocument()

    act(() => resources.invalidate(['dashboard']))
    await waitFor(() => expect(dashboardRead).toHaveBeenCalledTimes(1))
    // The cached settings stay usable while the refresh hangs, and a routine
    // re-read is not an outage: nothing flashes, nothing is demanded.
    expect(screen.getByText('Настройки копирования')).toBeInTheDocument()
    expect(screen.queryByLabelText('Загрузка настроек')).toBeNull()
    expect(screen.getByText('Всё работает')).toBeInTheDocument()
    expect(screen.queryByText('Данные устарели')).toBeNull()

    await act(async () => {
      refreshGate.reject(new ApiError(500, 'network_error', 'down'))
      await Promise.resolve()
    })
    // A genuinely failed background read keeps the same cached frame amber
    // instead of replacing it with a skeleton/error-only surface.
    expect(screen.getByText('Настройки копирования')).toBeInTheDocument()
    expect(screen.queryByLabelText('Загрузка настроек')).toBeNull()
    expect(screen.queryByText('Всё работает')).toBeNull()
    expect(screen.getByText('Данные устарели')).toBeInTheDocument()
  })

  it('fresh strong Status avoids an implicit read and exposes no routine refresh action', async () => {
    const resources = new MiniAppResources(AUTH, 'status-refresh')
    resources.publishConfirmed('status', statusDto())
    const status = vi.spyOn(api, 'status').mockResolvedValue(statusDto())

    render(<SystemStatusScreen auth={AUTH} resources={resources} />)
    expect(screen.getByText('Всё работает')).toBeInTheDocument()
    expect(status).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Обновить' })).toBeNull()
  })

  it('AccountBuilder stays weak on cached entry and uses refresh=true only after explicit recheck', async () => {
    const resources = new MiniAppResources(AUTH, 'account-refresh')
    const accountBuilder = vi
      .spyOn(api, 'accountBuilder')
      .mockResolvedValueOnce(accountBuilderDto())
      .mockResolvedValueOnce(accountBuilderDto({ builder_name: 'copytradeaster-new' }))

    await resources.ensure('accountBuilder', { strength: 'weak' })
    expect(accountBuilder).toHaveBeenCalledTimes(1)
    expect(accountBuilder.mock.calls[0]?.[1]).toBe(false)

    render(
      <AccountBuilderScreen
        auth={AUTH}
        resources={resources}
        accountMasked="0xAAAA…BBBB"
        onNotConnected={noop}
      />,
    )
    expect(screen.getByText('Builder подтверждён')).toBeInTheDocument()
    expect(accountBuilder).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Перепроверить разрешения' }))
    await screen.findByText('copytradeaster-new')
    expect(accountBuilder).toHaveBeenCalledTimes(2)
    expect(accountBuilder.mock.calls.map((call) => call[1])).toEqual([false, true])
  })
})
