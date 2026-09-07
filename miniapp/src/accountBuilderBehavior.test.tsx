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
import { act } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api, ApiError } from './api'
import { MiniAppResources } from './resources'
import * as wallet from './wallet'
import { isAccountBuilderVerified, isBuilderVerified } from './accountBuilder'
import { trustSummary } from './home'
import { NestedRoute } from './components/AppShell'
import { SettingsView } from './screens/SettingsScreen'
import { AccountBuilderScreen } from './screens/AccountBuilderScreen'
import { TRANSPORT_STALE_WARNING } from './systemStatus'
import { statusTrust } from './systemStatus'
import type {
  AccountBuilderDTO,
  BuilderPrepareDTO,
  ConfirmationDTO,
  DashboardDTO,
  StatusDTO,
} from './types'

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

  it('cached verified evidence turns amber at the Agent expiry while the screen stays mounted', async () => {
    vi.useFakeTimers()
    try {
      const now = 1_800_000_000_000
      vi.setSystemTime(now)
      const resources = new MiniAppResources(AUTH, 'account-expiry')
      resources.publishConfirmed(
        'accountBuilder',
        dto({ agent_expires_at_ms: now + 100 }),
      )
      const loader = vi.spyOn(api, 'accountBuilder')

      render(
        <AccountBuilderScreen
          auth={AUTH}
          resources={resources}
          accountMasked="0xAAAA…BBBB"
          onNotConnected={() => {}}
        />,
      )
      expect(screen.getByText('Builder подтверждён')).toBeDefined()
      expect(loader).not.toHaveBeenCalled()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(101)
      })
      expect(screen.queryByText('Builder подтверждён')).toBeNull()
      expect(screen.getByText('Данные устарели')).toBeDefined()
      expect(screen.getByText(TRANSPORT_STALE_WARNING)).toBeDefined()
      expect(loader).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
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

  it('failed refresh keeps data but drops the verified visual until retry succeeds', async () => {
    const loader = vi
      .spyOn(api, 'accountBuilder')
      .mockResolvedValueOnce(dto())
      .mockRejectedValueOnce(new ApiError(0, 'network_error', 'down'))
      .mockResolvedValueOnce(dto())
    renderScreen()
    await screen.findByText('Builder подтверждён')

    fireEvent.click(screen.getByRole('button', { name: 'Перепроверить разрешения' }))
    await screen.findByText(TRANSPORT_STALE_WARNING)
    // Shown data is not wiped ...
    expect(screen.getByText('0x2576…6EF8')).toBeDefined()
    // ... but the green verified visual is gone until a refresh succeeds.
    expect(screen.queryByText('Builder подтверждён')).toBeNull()
    await screen.findByText('Данные устарели')
    expect(document.querySelector('.status-hero')?.className).toContain('tone-warning')
    expect(document.querySelector('.status-hero')?.className).not.toContain('tone-success')
    expect(loader).toHaveBeenCalledTimes(2)

    // A successful retry restores verified and clears the warning.
    fireEvent.click(screen.getByRole('button', { name: 'Перепроверить разрешения' }))
    await screen.findByText('Builder подтверждён')
    expect(screen.queryByText(TRANSPORT_STALE_WARNING)).toBeNull()
    expect(loader).toHaveBeenCalledTimes(3)
    expect(loader).toHaveBeenLastCalledWith(AUTH, true, expect.any(Object))
  })

  it('post-reauthorization refresh is fenced and an explicit later refresh can advance it', async () => {
    const expiring = () =>
      dto({
        state: 'expiring',
        reason: 'agent_expiring',
        remediation: 'reauthorize',
        builder_name: 'proof-old',
      })
    const postGate = deferred<AccountBuilderDTO>()
    const loader = vi
      .spyOn(api, 'accountBuilder')
      .mockResolvedValueOnce(expiring())
      .mockReturnValueOnce(postGate.promise)
      .mockResolvedValueOnce(
        dto({ state: 'verified', reason: 'none', remediation: null, builder_name: 'proof-new' }),
      )
    vi.spyOn(api, 'reauthorizePrepare').mockResolvedValue({
      pending_id: 'reauth-1',
      eip712: { domain: {}, message: {}, types: {} },
    })
    vi.spyOn(api, 'reauthorizeSubmit').mockResolvedValue({
      account_id: 'tg-7',
      account_masked: '0x1111…1111',
      signer_masked: '0x2222…2222',
      builder_name: 'copytradeaster',
      max_fee_rate: '0.0001',
      agent_expires_at_ms: 2_000_000_000_000,
      paused: true,
    })
    vi.spyOn(wallet, 'createInjectedAdapter').mockReturnValue({
      kind: 'injected',
      connect: async () => '0xabc',
      switchToRegistrationChain: async () => {},
      signTypedData: async () => '0xsig',
    })
    renderScreen()
    await screen.findByText('Требуется переавторизация')

    fireEvent.click(screen.getByRole('button', { name: 'Переавторизовать' }))
    await screen.findByText('Подписать в кошельке')
    fireEvent.click(screen.getByRole('button', { name: 'Подписать в кошельке' }))

    // The post-submit authoritative refresh owns the mutation barrier. While
    // it is unresolved the screen keeps the session-level write lock, so a
    // second explicit check cannot race the post-write proof read.
    const refreshButton = screen.getByRole('button', { name: 'Перепроверить разрешения' })
    await waitFor(() => expect(loader).toHaveBeenCalledTimes(2))
    expect(refreshButton).toBeDisabled()

    postGate.resolve(expiring())
    await waitFor(() => expect(refreshButton).toBeEnabled())
    expect(screen.getByText('proof-old')).toBeDefined()

    fireEvent.click(refreshButton)
    await screen.findByText('proof-new')
    expect(loader).toHaveBeenCalledTimes(3)
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

  it('owner lock prevents duplicate reauthorize prepare across Back-style remount while pending', async () => {
    const resources = new MiniAppResources(AUTH, 'reauth-remount')
    resources.publishConfirmed(
      'accountBuilder',
      dto({ state: 'expiring', reason: 'agent_expiring', remediation: 'reauthorize' }),
    )
    const prepareGate = deferred<BuilderPrepareDTO>()
    const prepare = vi.spyOn(api, 'reauthorizePrepare').mockReturnValue(prepareGate.promise)
    vi.spyOn(wallet, 'createInjectedAdapter').mockReturnValue({
      kind: 'injected',
      connect: async () => '0xabc',
      switchToRegistrationChain: async () => {},
      signTypedData: async () => '0xsig',
    })

    const first = render(
      <AccountBuilderScreen auth={AUTH} resources={resources} onNotConnected={() => {}} />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Переавторизовать' }))
    await waitFor(() => expect(prepare).toHaveBeenCalledTimes(1))
    expect(resources.mutationLocked('account-builder')).toBe(true)
    first.unmount()

    render(<AccountBuilderScreen auth={AUTH} resources={resources} onNotConnected={() => {}} />)
    const reopened = screen.getByRole('button', { name: 'Переавторизовать' })
    expect(reopened).toBeDisabled()
    fireEvent.click(reopened)
    expect(prepare).toHaveBeenCalledTimes(1)

    prepareGate.resolve({ pending_id: 'reauth-pending', eip712: { domain: {}, message: {}, types: {} } })
    await waitFor(() => expect(resources.mutationLocked('account-builder')).toBe(false))
  })

  it('owner lock prevents duplicate disconnect begin across remount while pending', async () => {
    const resources = new MiniAppResources(AUTH, 'disconnect-remount')
    resources.publishConfirmed('accountBuilder', dto())
    const beginGate = deferred<ConfirmationDTO>()
    const begin = vi.spyOn(api, 'beginConfirmation').mockReturnValue(beginGate.promise)

    const first = render(
      <AccountBuilderScreen auth={AUTH} resources={resources} onNotConnected={() => {}} />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Отключить аккаунт' }))
    await waitFor(() => expect(begin).toHaveBeenCalledTimes(1))
    expect(resources.mutationLocked('account-builder')).toBe(true)
    first.unmount()

    render(<AccountBuilderScreen auth={AUTH} resources={resources} onNotConnected={() => {}} />)
    const reopened = screen.getByRole('button', { name: 'Отключить аккаунт' })
    expect(reopened).toBeDisabled()
    fireEvent.click(reopened)
    expect(begin).toHaveBeenCalledTimes(1)

    beginGate.resolve({ token: 'tok', action: 'disconnect', account_id: 'tg-7', expires_at: null })
    await waitFor(() => expect(resources.mutationLocked('account-builder')).toBe(false))
  })

  it('keeps reauthorize submit locked across remount until an ambiguous timeout is reconciled', async () => {
    const resources = new MiniAppResources(AUTH, 'reauth-ambiguous')
    resources.publishConfirmed(
      'accountBuilder',
      dto({ state: 'expiring', reason: 'agent_expiring', remediation: 'reauthorize' }),
    )
    vi.spyOn(api, 'reauthorizePrepare').mockResolvedValue({
      pending_id: 'reauth-ambiguous',
      eip712: { domain: {}, message: {}, types: {} },
    })
    const submit = vi
      .spyOn(api, 'reauthorizeSubmit')
      .mockRejectedValue(new ApiError(0, 'timeout_error', 'response lost'))
    const reconcile = deferred<AccountBuilderDTO>()
    const accountBuilder = vi.spyOn(api, 'accountBuilder').mockReturnValue(reconcile.promise)
    vi.spyOn(wallet, 'createInjectedAdapter').mockReturnValue({
      kind: 'injected',
      connect: async () => '0xabc',
      switchToRegistrationChain: async () => {},
      signTypedData: async () => '0xsig',
    })

    const first = render(
      <AccountBuilderScreen auth={AUTH} resources={resources} onNotConnected={() => {}} />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Переавторизовать' }))
    await screen.findByText('Подписать в кошельке')
    fireEvent.click(screen.getByRole('button', { name: 'Подписать в кошельке' }))
    await waitFor(() => expect(accountBuilder).toHaveBeenCalledTimes(1))
    expect(submit).toHaveBeenCalledTimes(1)
    expect(resources.mutationLocked('account-builder')).toBe(true)

    first.unmount()
    render(<AccountBuilderScreen auth={AUTH} resources={resources} onNotConnected={() => {}} />)
    const reopened = screen.getByRole('button', { name: 'Переавторизовать' })
    expect(reopened).toBeDisabled()
    fireEvent.click(reopened)
    expect(submit).toHaveBeenCalledTimes(1)

    reconcile.resolve(dto({ state: 'verified', reason: 'none', remediation: null }))
    await screen.findByText('Builder подтверждён')
    await waitFor(() => expect(resources.mutationLocked('account-builder')).toBe(false))
    expect(submit).toHaveBeenCalledTimes(1)
  })

  it('ambiguous reauthorize submit fences a pre-mutation strong attestation before recovery', async () => {
    const resources = new MiniAppResources(AUTH, 'reauth-causal-fence')
    const expiring = dto({
      state: 'expiring',
      reason: 'agent_expiring',
      remediation: 'reauthorize',
    })
    resources.publishConfirmed('accountBuilder', expiring)
    const oldGate = deferred<AccountBuilderDTO>()
    const currentGate = deferred<AccountBuilderDTO>()
    const accountBuilder = vi
      .spyOn(api, 'accountBuilder')
      .mockReturnValueOnce(oldGate.promise)
      .mockReturnValueOnce(currentGate.promise)
    vi.spyOn(api, 'reauthorizePrepare').mockResolvedValue({
      pending_id: 'reauth-causal-fence',
      eip712: { domain: {}, message: {}, types: {} },
    })
    const submit = vi
      .spyOn(api, 'reauthorizeSubmit')
      .mockRejectedValue(new ApiError(0, 'timeout_error', 'response lost'))
    vi.spyOn(wallet, 'createInjectedAdapter').mockReturnValue({
      kind: 'injected',
      connect: async () => '0xabc',
      switchToRegistrationChain: async () => {},
      signTypedData: async () => '0xsig',
    })

    // Owner-held explicit verification started before the reauthorization.
    // Its eventual non-verified result cannot reconcile the later submit.
    const oldRead = resources.refresh('accountBuilder', 'strong')
    render(
      <AccountBuilderScreen auth={AUTH} resources={resources} onNotConnected={() => {}} />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Переавторизовать' }))
    await screen.findByText('Подписать в кошельке')
    fireEvent.click(screen.getByRole('button', { name: 'Подписать в кошельке' }))
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1))

    oldGate.resolve(expiring)
    await oldRead
    await waitFor(() => expect(accountBuilder).toHaveBeenCalledTimes(2))
    expect(resources.mutationState('account-builder')).toBe('active')
    expect(screen.getByRole('button', { name: /Подтверждаем|Переавторизовать/ })).toBeDisabled()

    currentGate.resolve(dto({ state: 'verified', reason: 'none', remediation: null }))
    await screen.findByText('Builder подтверждён')
    await waitFor(() => expect(resources.mutationState('account-builder')).toBeNull())
    expect(submit).toHaveBeenCalledTimes(1)
  })

  it('keeps reauthorize fail-closed across remount, then strong attestation recovery admits at most one fresh prepare', async () => {
    const resources = new MiniAppResources(AUTH, 'reauth-double-unknown')
    resources.publishConfirmed(
      'accountBuilder',
      dto({ state: 'expiring', reason: 'agent_expiring', remediation: 'reauthorize' }),
    )
    const prepare = vi.spyOn(api, 'reauthorizePrepare').mockResolvedValue({
      pending_id: 'reauth-double-unknown',
      eip712: { domain: {}, message: {}, types: {} },
    })
    const submit = vi
      .spyOn(api, 'reauthorizeSubmit')
      .mockRejectedValue(new ApiError(0, 'timeout_error', 'response lost'))
    const accountBuilder = vi
      .spyOn(api, 'accountBuilder')
      .mockRejectedValueOnce(new ApiError(0, 'network_error', 'verify lost'))
      .mockResolvedValueOnce(
        dto({ state: 'expiring', reason: 'agent_expiring', remediation: 'reauthorize' }),
      )
    vi.spyOn(wallet, 'createInjectedAdapter').mockReturnValue({
      kind: 'injected',
      connect: async () => '0xabc',
      switchToRegistrationChain: async () => {},
      signTypedData: async () => '0xsig',
    })

    const first = render(
      <AccountBuilderScreen auth={AUTH} resources={resources} onNotConnected={() => {}} />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Переавторизовать' }))
    await screen.findByText('Подписать в кошельке')
    fireEvent.click(screen.getByRole('button', { name: 'Подписать в кошельке' }))
    await waitFor(() => expect(accountBuilder).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(resources.mutationLocked('account-builder')).toBe(true))
    expect(submit).toHaveBeenCalledTimes(1)

    first.unmount()
    render(<AccountBuilderScreen auth={AUTH} resources={resources} onNotConnected={() => {}} />)
    const reopened = screen.getByRole('button', { name: 'Переавторизовать' })
    expect(reopened).toBeDisabled()
    fireEvent.click(reopened)
    expect(submit).toHaveBeenCalledTimes(1)
    expect(resources.mutationLocked('account-builder')).toBe(true)
    expect(resources.mutationState('account-builder')).toBe('unknown')

    const check = screen.getByRole('button', { name: 'Перепроверить разрешения' })
    expect(check).toBeEnabled()
    fireEvent.click(check)
    await waitFor(() => expect(resources.mutationState('account-builder')).toBeNull())
    expect(submit).toHaveBeenCalledTimes(1)
    expect(accountBuilder).toHaveBeenCalledTimes(2)

    const retry = screen.getByRole('button', { name: 'Переавторизовать' })
    fireEvent.click(retry)
    fireEvent.click(retry)
    await waitFor(() => expect(prepare).toHaveBeenCalledTimes(2))
    expect(prepare).toHaveBeenCalledTimes(2)
  })

  it('recovers an unknown disconnect after remount from session truth and routes committed disconnect once', async () => {
    const resources = new MiniAppResources(AUTH, 'disconnect-double-unknown')
    resources.publishConfirmed('accountBuilder', dto())
    vi.spyOn(api, 'beginConfirmation').mockResolvedValue({
      token: 'disconnect-double-unknown',
      action: 'disconnect',
      account_id: 'tg-7',
      expires_at: null,
    })
    const confirm = vi
      .spyOn(api, 'confirmConfirmation')
      .mockRejectedValue(new ApiError(0, 'timeout_error', 'response lost'))
    const session = vi
      .spyOn(api, 'session')
      .mockRejectedValueOnce(new ApiError(0, 'network_error', 'verify lost'))
      .mockResolvedValueOnce({
        user_id: 7,
        access_state: 'approved',
        environment: 'testnet',
        connection_state: 'empty',
        account_connected: false,
      })
    const onNotConnected = vi.fn()

    const first = render(
      <AccountBuilderScreen auth={AUTH} resources={resources} onNotConnected={onNotConnected} />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Отключить аккаунт' }))
    await screen.findByText('Отключить аккаунт?')
    fireEvent.click(screen.getByRole('button', { name: 'Подтвердить отключение' }))
    await waitFor(() => expect(resources.mutationState('account-builder')).toBe('unknown'))
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(session).toHaveBeenCalledTimes(1)

    first.unmount()
    render(
      <AccountBuilderScreen auth={AUTH} resources={resources} onNotConnected={onNotConnected} />,
    )
    expect(screen.getByRole('button', { name: 'Отключить аккаунт' })).toBeDisabled()

    // Explicit authoritative recovery reads session state, does not replay the
    // destructive confirm, and routes the already-committed disconnect once.
    fireEvent.click(screen.getByRole('button', { name: 'Перепроверить разрешения' }))
    await waitFor(() => expect(onNotConnected).toHaveBeenCalledTimes(1))
    expect(session).toHaveBeenCalledTimes(2)
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(resources.mutationState('account-builder')).toBeNull()
  })

  it('keeps disconnect confirm locked until ambiguous transport is reconciled from session state', async () => {
    const resources = new MiniAppResources(AUTH, 'disconnect-ambiguous')
    resources.publishConfirmed('accountBuilder', dto())
    vi.spyOn(api, 'beginConfirmation').mockResolvedValue({
      token: 'disconnect-ambiguous',
      action: 'disconnect',
      account_id: 'tg-7',
      expires_at: null,
    })
    const confirm = vi
      .spyOn(api, 'confirmConfirmation')
      .mockRejectedValue(new ApiError(0, 'network_error', 'response lost'))
    const session = deferred<{
      user_id: number
      access_state: 'approved'
      environment: string
      connection_state: 'empty'
      account_connected: boolean
    }>()
    vi.spyOn(api, 'session').mockReturnValue(session.promise)
    const onNotConnected = vi.fn()

    const first = render(
      <AccountBuilderScreen auth={AUTH} resources={resources} onNotConnected={onNotConnected} />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Отключить аккаунт' }))
    await screen.findByText('Отключить аккаунт?')
    fireEvent.click(screen.getByRole('button', { name: 'Подтвердить отключение' }))
    await waitFor(() => expect(resources.mutationLocked('account-builder')).toBe(true))
    expect(confirm).toHaveBeenCalledTimes(1)

    first.unmount()
    render(<AccountBuilderScreen auth={AUTH} resources={resources} onNotConnected={onNotConnected} />)
    const reopened = screen.getByRole('button', { name: 'Отключить аккаунт' })
    expect(reopened).toBeDisabled()
    fireEvent.click(reopened)
    expect(confirm).toHaveBeenCalledTimes(1)

    session.resolve({
      user_id: 7,
      access_state: 'approved',
      environment: 'testnet',
      connection_state: 'empty',
      account_connected: false,
    })
    await waitFor(() => expect(onNotConnected).toHaveBeenCalledTimes(1))
    expect(confirm).toHaveBeenCalledTimes(1)
  })

  it('direct disconnect-confirm 401 hard-disposes the shared scope exactly once', async () => {
    const invalidSession = vi.fn()
    const resources = new MiniAppResources(AUTH, 'disconnect-401', invalidSession)
    resources.publishConfirmed('accountBuilder', dto())
    vi.spyOn(api, 'beginConfirmation').mockResolvedValue({
      token: 'disconnect-401',
      action: 'disconnect',
      account_id: 'tg-7',
      expires_at: null,
    })
    const confirm = vi
      .spyOn(api, 'confirmConfirmation')
      .mockRejectedValue(new ApiError(401, 'invalid_session', 'expired'))

    render(<AccountBuilderScreen auth={AUTH} resources={resources} onNotConnected={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Отключить аккаунт' }))
    await screen.findByText('Отключить аккаунт?')
    fireEvent.click(screen.getByRole('button', { name: 'Подтвердить отключение' }))

    await waitFor(() => expect(invalidSession).toHaveBeenCalledTimes(1))
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(resources.getSnapshot('accountBuilder').data).toBeNull()
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
