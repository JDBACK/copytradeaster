/** Behavioral regression tests for the System Status screen (P2.5).
 *
 * Static markup cannot prove the stale lifecycle: these tests mount the real
 * SystemStatusScreen in jsdom and assert the live transitions — loading →
 * fresh, refresh races, transport-stale honesty, blocked remediation, and
 * the hard-error path. The previous DTO is local component state only. */

// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { renderToStaticMarkup } from 'react-dom/server'
import { api, ApiError } from './api'
import { MiniAppResources } from './resources'
import { NestedRoute } from './components/AppShell'
import { SystemStatusScreen, SystemStatusView } from './screens/SystemStatusScreen'
import { TRANSPORT_STALE_WARNING } from './systemStatus'
import type { StatusDTO } from './types'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  delete (window as unknown as { Telegram?: unknown }).Telegram
})

const AUTH = { initData: 'test' }

function dto(overrides: Partial<StatusDTO> = {}): StatusDTO {
  return {
    state: 'ready',
    system_state: 'ready',
    copy_state: 'paused',
    aster_state: 'ok',
    permissions_state: 'valid',
    account_masked: '0x3c45…F975',
    signer_masked: '0x2222…2222',
    positions_count: 2,
    permissions: { can_read: true, can_perp_trade: true, can_withdraw: false },
    snapshot_captured_at: '2026-09-03T12:00:00+00:00',
    last_check_at: '2026-09-03T12:00:00+00:00',
    age_seconds: 5,
    reason: 'none',
    remediation: null,
    ...overrides,
  }
}

function staleDto(): StatusDTO {
  return dto({
    state: 'stale',
    aster_state: 'stale',
    permissions_state: 'stale',
    positions_count: 2,
    age_seconds: 120,
    reason: 'snapshot_stale',
    remediation: 'refresh',
  })
}

function blockedDto(): StatusDTO {
  return dto({
    state: 'blocked',
    permissions_state: 'blocked',
    reason: 'permission_invalid',
    remediation: 'open_bot',
  })
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

function mockTelegramClose() {
  const close = vi.fn()
  ;(window as unknown as { Telegram?: unknown }).Telegram = { WebApp: { close } }
  return close
}

describe('SystemStatusScreen behavior', () => {
  it('loading keeps geometry with no green healthy placeholder', async () => {
    const gate = deferred<StatusDTO>()
    vi.spyOn(api, 'status').mockReturnValue(gate.promise)
    render(<SystemStatusScreen auth={AUTH} />)

    expect(screen.getByLabelText('Загрузка статуса')).toBeDefined()
    expect(screen.queryByText('Всё работает')).toBeNull()

    gate.resolve(dto())
    await screen.findByText('Всё работает')
  })

  it('initial open runs a real refresh and fresh shows all six rows', async () => {
    const loader = vi.spyOn(api, 'status').mockResolvedValue(dto())
    render(<SystemStatusScreen auth={AUTH} />)

    await screen.findByText('Всё работает')
    expect(loader).toHaveBeenCalledWith(AUTH, true, expect.any(Object))
    for (const label of [
      'Система',
      'Копирование',
      'Aster',
      'Разрешения',
      'Позиции',
      'Последняя проверка',
    ]) {
      expect(screen.getByText(label)).toBeDefined()
    }
    expect(screen.getByText('Подключён')).toBeDefined()
    expect(screen.getByText('Подтверждены')).toBeDefined()
  })

  it('healthy status has no routine refresh action', async () => {
    const loader = vi.spyOn(api, 'status').mockResolvedValue(dto())
    render(<SystemStatusScreen auth={AUTH} />)
    await screen.findByText('Всё работает')
    expect(loader).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('button', { name: 'Обновить' })).toBeNull()
  })

  it('uses the live authoritative cache for steady reads but keeps explicit verification strong', async () => {
    const resources = new MiniAppResources(AUTH, 'status-live-cache')
    vi.spyOn(resources, 'getLiveSnapshot').mockReturnValue({
      health: 'live',
      revision: 3,
      lastUpdatedAt: Date.now(),
      retrying: false,
    })
    const loader = vi.spyOn(api, 'status').mockResolvedValue(dto())

    render(<SystemStatusScreen auth={AUTH} resources={resources} />)
    await screen.findByText('Всё работает')
    // Steady state reads the already-authoritative process cache.
    expect(loader.mock.calls[0]?.slice(0, 2)).toEqual([AUTH, false])
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('backend stale keeps values visible with an amber note, never healthy', async () => {
    vi.spyOn(api, 'status').mockResolvedValue(staleDto())
    render(<SystemStatusScreen auth={AUTH} />)
    await screen.findByLabelText('Состояние: Данные устарели')
    // Last available values stay on screen.
    expect(screen.getByText('2')).toBeDefined()
    expect(screen.queryByText('Всё работает')).toBeNull()
    expect(screen.getByText('Снимок позиций устарел.')).toBeDefined()
  })

  it('degraded keeps values visible and asks the user for nothing', async () => {
    vi.spyOn(api, 'status').mockResolvedValue(
      dto({ state: 'degraded', reason: 'system_degraded', remediation: 'refresh' }),
    )
    render(<SystemStatusScreen auth={AUTH} />)

    await screen.findByText('Требуется проверка')
    expect(screen.getByText('Система требует проверки.')).toBeDefined()
    // Remediation "refresh" is a transport concern: the read heals by itself
    // (bounded retry) and the pull-down forces it, so no button is offered.
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.queryByText('Всё работает')).toBeNull()
  })

  it('blocked shows the typed reason with exactly one remediation action', async () => {
    const close = mockTelegramClose()
    vi.spyOn(api, 'status').mockResolvedValue(blockedDto())
    render(<SystemStatusScreen auth={AUTH} />)

    await screen.findByText('Требуется действие')
    expect(screen.getByText('Права подключения отклонены. Продолжите в чате с ботом.')).toBeDefined()
    const buttons = screen.getAllByRole('button')
    expect(buttons).toHaveLength(1)
    expect(buttons[0]).toHaveTextContent('Открыть бота')
    fireEvent.click(buttons[0])
    expect(close).toHaveBeenCalledTimes(1)
  })

  it('unknown remediation never executes', async () => {
    const close = mockTelegramClose()
    vi.spyOn(api, 'status').mockResolvedValue(
      dto({ state: 'blocked', reason: 'copy_blocked', remediation: 'launch_rocket' as never }),
    )
    render(<SystemStatusScreen auth={AUTH} />)

    await screen.findByText('Требуется действие')
    expect(screen.queryByRole('button')).toBeNull()
    expect(close).not.toHaveBeenCalled()
  })

  it('a failed first read heals itself without any manual retry', async () => {
    const loader = vi
      .spyOn(api, 'status')
      .mockRejectedValueOnce(new ApiError(0, 'network_error', 'down'))
      .mockResolvedValueOnce(dto())
    render(<SystemStatusScreen auth={AUTH} />)

    await screen.findByText('Нет связи')
    expect(screen.queryByText('Статус системы', { selector: 'h1' })).toBeDefined()
    expect(screen.queryByRole('button')).toBeNull()
    // No tap anywhere: the bounded self-healing retry re-reads on its own.
    await screen.findByText('Всё работает')
    expect(loader.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('network failure keeps the snapshot with a transport-stale warning', async () => {
    const resources = new MiniAppResources(AUTH, 'status-reconnect')
    resources.publishConfirmed('status', dto())
    resources.invalidate(['status'])
    const loader = vi.spyOn(api, 'status')
      .mockRejectedValueOnce(new ApiError(0, 'network_error', 'down'))
      .mockResolvedValueOnce(dto())
    const { rerender } = render(
      <SystemStatusScreen auth={AUTH} resources={resources} foregroundSignal={0} />,
    )

    await screen.findByText(TRANSPORT_STALE_WARNING)
    await waitFor(() => expect(loader.mock.calls.length).toBeGreaterThanOrEqual(1))
    // Previous data survives, but the old ready no longer looks fresh.
    expect(screen.getByText('2')).toBeDefined()
    expect(screen.queryByText('Всё работает')).toBeNull()
    expect(screen.getByText('Данные устарели')).toBeDefined()
    expect(screen.queryByRole('button', { name: 'Обновить' })).toBeNull()

    // Foreground recovery replaces the removed routine refresh action.
    rerender(<SystemStatusScreen auth={AUTH} resources={resources} foregroundSignal={1} />)
    await screen.findByText('Всё работает')
    expect(loader.mock.calls.length).toBeGreaterThanOrEqual(2)
    expect(screen.queryByText(TRANSPORT_STALE_WARNING)).toBeNull()
  })

  it('marks cached Status as stale while the live transport is down and clears on recovery', async () => {
    const resources = new MiniAppResources(AUTH, 'status-outage')
    resources.publishConfirmed('status', dto())
    const live = { health: 'stale' as 'live' | 'stale', revision: 1, lastUpdatedAt: 0, retrying: true }
    vi.spyOn(resources, 'getLiveSnapshot').mockImplementation(() => live)

    const { rerender } = render(<SystemStatusScreen auth={AUTH} resources={resources} />)
    await screen.findByText(TRANSPORT_STALE_WARNING)
    expect(screen.queryByText('Всё работает')).toBeNull()
    expect(screen.getByText('Данные устарели')).toBeDefined()

    // The channel recovers: the same cached DTO is trustworthy again and the
    // warning clears without the user doing anything.
    live.health = 'live'
    rerender(<SystemStatusScreen auth={AUTH} resources={resources} />)
    await screen.findByText('Всё работает')
    expect(screen.queryByText(TRANSPORT_STALE_WARNING)).toBeNull()
  })

  it('a late request never overwrites a newer refresh result', async () => {
    const first = deferred<StatusDTO>()
    const second = deferred<StatusDTO>()
    const loader = vi
      .spyOn(api, 'status')
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
    const { rerender } = render(<SystemStatusScreen auth={AUTH} />)
    // A new auth context starts a newer load while the first is in flight.
    rerender(<SystemStatusScreen auth={{ initData: 'other' }} />)
    expect(loader).toHaveBeenCalledTimes(2)

    second.resolve(dto({ positions_count: 7 }))
    await screen.findByText('7')
    first.resolve(dto({ positions_count: 1 }))
    // Let the stale winner settle without touching the visible snapshot.
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(screen.getByText('7')).toBeDefined()
    expect(screen.queryByText('1')).toBeNull()
  })

  it('diagnostic rows are read-only with no chevrons or hidden actions', () => {
    const html = renderToStaticMarkup(
      <SystemStatusView
        dto={dto()}
        transportStale={false}
        onOpenBot={() => {}}
      />,
    )
    expect(html).not.toContain('settings-chevron')
    // Scope to the diagnostics section: nothing clickable may hide inside it.
    const diagnostics = (html.split('aria-label="Диагностика"')[1] ?? '').split('</section>')[0]
    expect(diagnostics).not.toContain('<button')
    expect(diagnostics).not.toContain('<a ')
  })

  it('Builder stays neutral and unverified', async () => {
    vi.spyOn(api, 'status').mockResolvedValue(dto())
    render(<SystemStatusScreen auth={AUTH} />)
    await screen.findByText('Всё работает')
    expect(screen.getByLabelText('Builder: не проверено')).toBeDefined()
    expect(screen.queryByText('Проверено')).toBeNull()
  })

  it('nested system-status renders through the typed stack', async () => {
    vi.spyOn(api, 'status').mockResolvedValue(dto())
    render(<NestedRoute nested={{ kind: 'system-status' }} auth={AUTH} />)
    await screen.findByText('Всё работает')
  })
})
