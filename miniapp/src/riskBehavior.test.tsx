/** Behavioral regression tests for the Risk on Stop screen (P2.4 corrective).
 *
 * Static markup cannot prove interaction: these tests mount the real
 * RiskScreen in jsdom, drive tap → dirty → save through @testing-library,
 * and assert the mutation contract — exactly one POST, server response
 * becomes confirmed, failures keep confirmed, toast only on success, the
 * ruler frozen mid-save, and the legacy off-grid banner. */

 // @vitest-environment jsdom

import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { RiskScreen } from './screens/RiskScreen'
import { api, ApiError } from './api'
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
})
