/** Behavioral regression tests for the Account + Builder screen (P2.6).
 *
 * Mounts the real AccountBuilderScreen in jsdom and asserts the live
 * transitions with the existing infrastructure only: loading → verified,
 * expiring + backend-gated reauthorize, stale snapshot preservation,
 * unavailable/blocked/direct states, refresh transitions without data loss,
 * destructive disconnect confirmation, duplicate-tap guards, and the shared
 * trust verdict across Account/Home/System Status. */

// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { api, ApiError } from './api'
import { isAccountBuilderVerified, isBuilderVerified } from './accountBuilder'
import { trustSummary } from './home'
import { NestedRoute } from './components/AppShell'
import { SettingsView } from './screens/SettingsScreen'
import { AccountBuilderScreen } from './screens/AccountBuilderScreen'
import { TRANSPORT_STALE_WARNING } from './systemStatus'
import { statusTrust } from './systemStatus'
import type { AccountBuilderDTO, DashboardDTO, StatusDTO } from './types'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const AUTH = { initData: 'test' }

function dto(overrides: Partial<AccountBuilderDTO> = {}): AccountBuilderDTO {
  return {
    identity_source: 'builder',
    state: 'verified',
    reason: 'none',
    remediation: null,
    account_masked: '0x1111…1111',
    signer_masked: '0x2222…2222',
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

function renderScreen(onNotConnected = () => {}) {
  return render(
    <AccountBuilderScreen auth={AUTH} accountMasked="0xAAAA…BBBB" onNotConnected={onNotConnected} />,
  )
}

describe('AccountBuilderScreen behavior', () => {
  it('loading keeps geometry with no verified placeholder', async () => {
    const gate = deferred<AccountBuilderDTO>()
    vi.spyOn(api, 'accountBuilder').mockReturnValue(gate.promise)
    renderScreen()

    expect(screen.getByLabelText('Загрузка аккаунта')).toBeDefined()
    expect(screen.queryByText('Builder подтверждён')).toBeNull()

    gate.resolve(dto())
    await screen.findByText('Builder подтверждён')
  })

  it('verified shows masked identities, fee cap, permissions and expiry', async () => {
    vi.spyOn(api, 'accountBuilder').mockResolvedValue(dto())
    renderScreen()

    await screen.findByText('Builder подтверждён')
    expect(screen.getByText('0x1111…1111')).toBeDefined()
    expect(screen.getByText('0x2222…2222')).toBeDefined()
    expect(screen.getByText('0x2576…6EF8')).toBeDefined()
    expect(screen.getByText('copytradeaster')).toBeDefined()
    // No full addresses anywhere on screen.
    expect(document.body.textContent).not.toContain('0x1111111111111111111111111111111111111111')
    // Reauthorize is absent without backend remediation.
    expect(screen.queryByText('Переавторизовать')).toBeNull()
    expect(screen.getByText('Перепроверить разрешения')).toBeDefined()
    expect(screen.getByText('Отключить аккаунт')).toBeDefined()
  })

  it('expiring shows reauthorize only from backend remediation', async () => {
    vi.spyOn(api, 'accountBuilder').mockResolvedValue(
      dto({ state: 'expiring', reason: 'agent_expiring', remediation: 'reauthorize' }),
    )
    renderScreen()

    await screen.findByText('Переавторизовать')
    expect(screen.getByText('Требуется переавторизация')).toBeDefined()
  })

  it('stale preserves the snapshot and never looks verified', async () => {
    vi.spyOn(api, 'accountBuilder').mockResolvedValue(
      dto({ state: 'stale', reason: 'transient_unavailable', remediation: 'refresh' }),
    )
    renderScreen()

    await screen.findByText('Данные устарели')
    expect(screen.queryByText('Builder подтверждён')).toBeNull()
    // Last-known evidence stays visible.
    expect(screen.getByText('0x2576…6EF8')).toBeDefined()
    expect(screen.getByText('copytradeaster')).toBeDefined()
  })

  it('unavailable, blocked and direct states render honestly', async () => {
    const loader = vi.spyOn(api, 'accountBuilder')
    loader.mockResolvedValueOnce(dto({ state: 'unavailable', reason: 'snapshot_missing' }))
    const { unmount } = renderScreen()
    await screen.findByText('Проверка недоступна')
    unmount()
    cleanup()

    loader.mockResolvedValueOnce(
      dto({ state: 'blocked', reason: 'builder_mismatch', remediation: 'reauthorize' }),
    )
    renderScreen()
    await screen.findByText('Требуется переавторизация')
    await screen.findByText('Переавторизовать')
    cleanup()

    loader.mockResolvedValueOnce(
      dto({
        identity_source: 'direct',
        state: 'not_applicable',
        reason: 'direct_account',
        builder_address_masked: null,
        builder_name: null,
        max_fee_rate: null,
      }),
    )
    renderScreen()
    await screen.findByText('Прямое подключение')
    expect(screen.getByText('Не используется')).toBeDefined()
    expect(screen.queryByText('Переавторизовать')).toBeNull()
  })

  it('failed refresh keeps shown data with a transport-stale marker', async () => {
    const loader = vi
      .spyOn(api, 'accountBuilder')
      .mockResolvedValueOnce(dto())
      .mockRejectedValueOnce(new ApiError(0, 'network_error', 'down'))
    renderScreen()
    await screen.findByText('Builder подтверждён')

    fireEvent.click(screen.getByRole('button', { name: 'Перепроверить разрешения' }))
    await screen.findByText(TRANSPORT_STALE_WARNING)
    // Shown data is not wiped.
    expect(screen.getByText('Builder подтверждён')).toBeDefined()
    expect(screen.getByText('0x2576…6EF8')).toBeDefined()
    expect(loader).toHaveBeenCalledTimes(2)
    expect(loader).toHaveBeenLastCalledWith(AUTH, true)
  })

  it('duplicate refresh taps never start two mutations', async () => {
    const gate = deferred<AccountBuilderDTO>()
    const loader = vi
      .spyOn(api, 'accountBuilder')
      .mockResolvedValueOnce(dto())
      .mockReturnValue(gate.promise)
    renderScreen()
    await screen.findByText('Builder подтверждён')

    const button = screen.getByRole('button', { name: 'Перепроверить разрешения' })
    fireEvent.click(button)
    fireEvent.click(button)
    expect(loader).toHaveBeenCalledTimes(2)

    gate.resolve(dto())
    await screen.findByText('Builder подтверждён')
  })

  it('destructive disconnect requires confirmation and returns to onboarding', async () => {
    vi.spyOn(api, 'accountBuilder').mockResolvedValue(dto())
    const begin = vi.spyOn(api, 'beginConfirmation').mockResolvedValue({
      token: 'tok',
      action: 'disconnect',
      account_id: 'tg-7',
      expires_at: null,
    })
    const confirm = vi.spyOn(api, 'confirmConfirmation').mockResolvedValue({} as DashboardDTO)
    const onNotConnected = vi.fn()
    renderScreen(onNotConnected)

    await screen.findByText('Builder подтверждён')
    fireEvent.click(screen.getByRole('button', { name: 'Отключить аккаунт' }))
    // Sheet states the local-only semantics before any mutation.
    await screen.findByText('Отключить аккаунт?')
    expect(screen.getByText(/не отзывается/)).toBeDefined()
    expect(begin).toHaveBeenCalledWith(AUTH, 'disconnect')

    fireEvent.click(screen.getByRole('button', { name: 'Подтвердить отключение' }))
    await screen.findByText('Builder подтверждён')
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(onNotConnected).toHaveBeenCalledTimes(1)
  })

  it('settings row opens the nested Account + Builder screen', () => {
    const onOpen = vi.fn()
    render(
      <SettingsView
        dashboard={
          {
            copy_state: 'paused',
            system_state: 'ready',
            account_masked: '0xAAAA…BBBB',
            notification_enabled: true,
            is_admin: false,
          } as DashboardDTO
        }
        accountMasked="0xAAAA…BBBB"
        onOpen={onOpen}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /Аккаунт и Builder/ }))
    expect(onOpen).toHaveBeenCalledWith({ kind: 'account-builder' })
  })

  it('nested route renders the screen and root tabs stay exactly four', async () => {
    vi.spyOn(api, 'accountBuilder').mockResolvedValue(dto())
    render(<NestedRoute nested={{ kind: 'account-builder' }} auth={AUTH} />)
    await screen.findByText('Builder подтверждён')

    const { ROOT_TABS } = await import('./navigation')
    expect(ROOT_TABS).toEqual(['home', 'positions', 'activity', 'settings'])
  })

  it('one authoritative verdict feeds Account, Home and System Status', () => {
    const account = dto()
    const dashboard = { builder_verified: true } as DashboardDTO
    const status = { builder_verified: true } as StatusDTO

    expect(isAccountBuilderVerified(account)).toBe(true)
    expect(isBuilderVerified(dashboard)).toBe(true)
    expect(trustSummary(dashboard).find((item) => item.key === 'builder')?.done).toBe(true)
    expect(statusTrust(status).find((item) => item.key === 'builder')?.done).toBe(true)

    const staleDashboard = { builder_verified: false } as DashboardDTO
    const staleStatus = { builder_verified: false } as StatusDTO
    expect(trustSummary(staleDashboard).find((item) => item.key === 'builder')?.done).toBe(false)
    expect(statusTrust(staleStatus).find((item) => item.key === 'builder')?.done).toBe(false)
    expect(isAccountBuilderVerified(dto({ state: 'stale' }))).toBe(false)
  })
})
