/** Behavioral regression tests for the Risk on Stop screen (P2.4 corrective).
 *
 * Static markup cannot prove interaction: these tests mount the real
 * RiskScreen in jsdom, drive tap → dirty → save through @testing-library,
 * and assert the mutation contract — exactly one POST, server response
 * becomes confirmed, failures keep confirmed, toast only on success, the
 * ruler frozen mid-save, and the legacy off-grid banner. */

 // @vitest-environment jsdom

import '@testing-library/jest-dom/vitest'
import { act } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { RiskScreen } from './screens/RiskScreen'
import { api, ApiError } from './api'
import { MiniAppResources } from './resources'
import type { RiskDTO } from './types'

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
})

const AUTH = { initData: 'test' }

function dto(overrides: Partial<RiskDTO> = {}): RiskDTO {
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

async function renderReady(server: RiskDTO): Promise<void> {
  vi.spyOn(api, 'risk').mockResolvedValue(server)
  render(<RiskScreen auth={AUTH} />)
  // Loading frame resolves once the DTO arrives.
  await screen.findByRole('button', { name: /сохранить/i })
}

describe('RiskScreen behavior (tap → dirty → save)', () => {
  it('tap selects, save sends exactly one POST, server response becomes confirmed + toast', async () => {
    await renderReady(dto())
    const save = screen.getByRole('button', { name: /сохранить/i })
    expect(save).toBeDisabled()

    fireEvent.click(screen.getByRole('radio', { name: '3,0%' }))
    expect(screen.getByRole('button', { name: /сохранить 3,0%/i })).toBeEnabled()
    expect(screen.getByText(/после 3,0%/i)).toBeDefined()

    const setter = vi.spyOn(api, 'setRiskPercent').mockResolvedValue(dto({ risk_percent: '0.03' }))
    // Double tap in the same tick: the gate admits exactly one mutation.
    // (The label flips to "Сохранение…" after the first tap, so re-querying
    // by name would miss — click the captured button twice instead.)
    const saveBtn = screen.getByRole('button', { name: /сохранить 3,0%/i })
    fireEvent.click(saveBtn)
    fireEvent.click(saveBtn)
    await screen.findByText(/сохранено · 3,0%/i)

    expect(setter).toHaveBeenCalledTimes(1)
    expect(setter).toHaveBeenCalledWith(AUTH, '0.03')
    expect(screen.getByRole('button', { name: /сохранить/i })).toBeDisabled()
    expect(screen.getByText(/сейчас 3,0%/i)).toBeDefined()
  })

  it('failed save keeps the confirmed value and shows no toast', async () => {
    await renderReady(dto())
    fireEvent.click(screen.getByRole('radio', { name: '2,0%' }))
    vi.spyOn(api, 'setRiskPercent').mockRejectedValue(new ApiError(500, 'network_error', ' net down'))

    fireEvent.click(screen.getByRole('button', { name: /сохранить 2,0%/i }))
    await screen.findByText(/настройка не изменилась/i)

    expect(screen.queryByText(/сохранено/i)).toBeNull()
    expect(screen.getByText(/сейчас 1,0%/i)).toBeDefined()
    // Still dirty — retry is possible without re-picking.
    expect(screen.getByRole('button', { name: /сохранить 2,0%/i })).toBeEnabled()
  })

  it('routes a direct mutation 401 through the session resource hard boundary', async () => {
    const onInvalidSession = vi.fn()
    const resources = new MiniAppResources(AUTH, 'risk-invalid-session', onInvalidSession)
    resources.publishConfirmed('risk', dto())
    vi.spyOn(api, 'setRiskPercent').mockRejectedValue(
      new ApiError(401, 'unauthorized', 'expired session'),
    )

    render(<RiskScreen auth={AUTH} resources={resources} />)
    fireEvent.click(screen.getByRole('radio', { name: '2,0%' }))
    fireEvent.click(screen.getByRole('button', { name: /сохранить 2,0%/i }))

    await act(async () => {})
    expect(onInvalidSession).toHaveBeenCalledTimes(1)
    expect(resources.getSnapshot('risk').data).toBeNull()
    expect(resources.mutationLocked('risk')).toBe(false)
    expect(screen.queryByText(/настройка не изменилась/i)).toBeNull()
  })

  it('freezes the ruler while the save POST is in flight', async () => {
    await renderReady(dto())
    fireEvent.click(screen.getByRole('radio', { name: '2,0%' }))

    let release!: (value: RiskDTO) => void
    const setter = vi
      .spyOn(api, 'setRiskPercent')
      .mockImplementation(() => new Promise<RiskDTO>((resolve) => (release = resolve)))
    fireEvent.click(screen.getByRole('button', { name: /сохранить 2,0%/i }))

    // Every tick is disabled mid-save: no re-pick can land under the POST.
    for (const tick of screen.getAllByRole('radio')) {
      expect(tick).toBeDisabled()
    }
    expect(screen.getByRole('radiogroup')).toHaveAttribute('aria-disabled', 'true')

    release(dto({ risk_percent: '0.02' }))
    await screen.findByText(/сохранено · 2,0%/i)
    expect(setter).toHaveBeenCalledTimes(1)
    for (const tick of screen.getAllByRole('radio')) {
      expect(tick).not.toBeDisabled()
    }
  })

  it('drops scroll/settle work scheduled just before the save POST locks the ruler', async () => {
    await renderReady(dto())
    fireEvent.click(screen.getByRole('radio', { name: '2,0%' }))
    // Let the tap cooldown expire so the scroll below schedules real work.
    await new Promise((resolve) => setTimeout(resolve, 200))

    let release!: (value: RiskDTO) => void
    const setter = vi
      .spyOn(api, 'setRiskPercent')
      .mockImplementation(() => new Promise<RiskDTO>((resolve) => (release = resolve)))

    // Race: a scroll frame queues an rAF repaint + trailing settle, and the
    // save tap lands before either fires.
    fireEvent.scroll(screen.getByRole('radiogroup'))
    fireEvent.click(screen.getByRole('button', { name: /сохранить 2,0%/i }))

    // Let every stale callback fire if it is still alive.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 250))
    })

    // Selection survived the freeze: the POST still carries the pre-save pick
    // (without the fix, the stale settle would have flipped it to 0,5%).
    expect(screen.getByText(/после 2,0%/i)).toBeDefined()
    expect(screen.queryByText(/после 0,5%/i)).toBeNull()
    release(dto({ risk_percent: '0.02' }))
    await screen.findByText(/сохранено · 2,0%/i)
    expect(setter).toHaveBeenCalledTimes(1)
    expect(setter).toHaveBeenCalledWith(AUTH, '0.02')
  })

  it('legacy stored value off the grid shows the banner and asks for an explicit save', async () => {
    const setter = vi.spyOn(api, 'setRiskPercent').mockResolvedValue(dto({ risk_percent: '0.01' }))
    // 1,2% predates the 0,5%-step grid: provably absent on the live DB, but
    // the screen must not strand it — banner + nearest pre-select, no silent rewrite.
    await renderReady(dto({ risk_percent: '0.012' }))

    expect(screen.getByText(/такое значение больше недоступно/i)).toBeDefined()
    expect(screen.getByText(/сейчас 1,2%/i)).toBeDefined()
    expect(setter).not.toHaveBeenCalled()
    const save = screen.getByRole('button', { name: /сохранить 1,0%/i })
    expect(save).toBeEnabled()

    fireEvent.click(save)
    await screen.findByText(/сохранено · 1,0%/i)
    expect(setter).toHaveBeenCalledTimes(1)
    expect(setter).toHaveBeenCalledWith(AUTH, '0.01')
    expect(screen.queryByText(/больше недоступно/i)).toBeNull()
  })

  it('preserves a dirty draft across an external policy update and blocks an invalid selection', async () => {
    const resources = new MiniAppResources(AUTH, 'risk-policy-conflict')
    resources.publishConfirmed('risk', dto())
    render(<RiskScreen auth={AUTH} resources={resources} />)
    await screen.findByRole('button', { name: /сохранить/i })

    fireEvent.click(screen.getByRole('radio', { name: '3,0%' }))
    expect(screen.getByText(/после 3,0%/i)).toBeDefined()

    await act(async () => {
      resources.publishConfirmed(
        'risk',
        dto({ max: '0.02', presets: ['0.01', '0.02'] }),
      )
    })

    // Server truth changed, but the user's unsaved draft is never silently
    // rewritten. It becomes an explicit conflict and cannot be submitted.
    expect(screen.getByText(/параметры риска изменились на сервере/i)).toBeDefined()
    expect(screen.getByText(/после 3,0%/i)).toBeDefined()
    expect(screen.getByRole('button', { name: /сохранить 3,0%/i })).toBeDisabled()
  })

  it('retains double-unknown Risk across remount until authoritative recovery without replay', async () => {
    const resources = new MiniAppResources(AUTH, 'risk-double-unknown')
    const onSaved = vi.fn()
    resources.publishConfirmed('risk', dto())
    const setter = vi
      .spyOn(api, 'setRiskPercent')
      .mockRejectedValue(new ApiError(0, 'timeout_error', 'response lost'))
    const reader = vi
      .spyOn(api, 'risk')
      .mockRejectedValueOnce(new ApiError(0, 'network_error', 'verify lost'))
      .mockResolvedValueOnce(dto())

    const first = render(<RiskScreen auth={AUTH} resources={resources} onSaved={onSaved} />)
    fireEvent.click(screen.getByRole('radio', { name: '2,0%' }))
    fireEvent.click(screen.getByRole('button', { name: /сохранить 2,0%/i }))
    await waitFor(() => expect(resources.mutationState('risk')).toBe('unknown'))
    expect(setter).toHaveBeenCalledTimes(1)
    first.unmount()

    render(<RiskScreen auth={AUTH} resources={resources} onSaved={onSaved} />)
    expect(screen.getByRole('button', { name: /сохранение/i })).toBeDisabled()
    // No button: the failed verification read retries on its own and the
    // post-boundary authoritative answer clears the retained uncertainty.
    await waitFor(() => expect(reader).toHaveBeenCalledTimes(2), { timeout: 3000 })
    await waitFor(() => expect(resources.mutationState('risk')).toBeNull())
    expect(setter).toHaveBeenCalledTimes(1)
    expect(onSaved).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: /сохранить/i })).toBeDisabled()
  })

  it('does not block a save for a routine revalidation, but does during an outage', async () => {
    const resources = new MiniAppResources(AUTH, 'risk-stale-honesty')
    resources.publishConfirmed('risk', dto())
    resources.invalidate(['risk'])
    vi.spyOn(api, 'risk').mockReturnValue(new Promise<RiskDTO>(() => {}))
    vi.spyOn(resources, 'getLiveSnapshot').mockReturnValue({
      health: 'live', revision: 1, lastUpdatedAt: Date.now(), retrying: false,
    })

    const first = render(<RiskScreen auth={AUTH} resources={resources} />)
    expect(screen.queryByText(/данные требуют проверки перед сохранением/i)).toBeNull()
    first.unmount()

    // A transport that is genuinely down still requires verified data first.
    const offline = new MiniAppResources(AUTH, 'risk-offline-honesty')
    offline.publishConfirmed('risk', dto())
    vi.spyOn(offline, 'getLiveSnapshot').mockReturnValue({
      health: 'stale', revision: 1, lastUpdatedAt: 0, retrying: true,
    })
    render(<RiskScreen auth={AUTH} resources={offline} />)
    expect(screen.getByText(/данные требуют проверки перед сохранением/i)).toBeDefined()
    fireEvent.click(screen.getByRole('radio', { name: '2,0%' }))
    expect(screen.getByRole('button', { name: /сохранить 2,0%/i })).toBeDisabled()
  })
})
