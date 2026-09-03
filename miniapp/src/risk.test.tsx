/** Tests for the Risk on Stop screen (P2.4, snap-strip redesign):
 * policy-grid enumeration over the backend-owned policy plus the static
 * static render contract of the production ruler.  A ruler position is valid by
 * construction — there is no manual input and no invalid-input class. */

import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { NestedRoute } from './components/AppShell'
import { RiskScreen, RiskRuler } from './screens/RiskScreen'
import {
  RISK_DEMO_BALANCE_USDT,
  demoLossUsdt,
  enumerateRiskSteps,
  formatRiskPercent,
  isRiskAllowed,
  isRiskDirty,
  nearestRiskStep,
} from './riskView'
import type { RiskDTO } from './types'

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

describe('enumerateRiskSteps (DTO grid → strip positions, no float)', () => {
  it('enumerates the exact current policy grid without float drift', () => {
    expect(enumerateRiskSteps(dto())).toEqual([
      '0.005',
      '0.01',
      '0.015',
      '0.02',
      '0.025',
      '0.03',
      '0.035',
      '0.04',
      '0.045',
      '0.05',
    ])
  })

  it('derives everything from the DTO, never from a hardcoded policy', () => {
    const narrow = dto({ min: '0.02', max: '0.04', step: '0.01', presets: ['0.02', '0.03', '0.04'] })
    expect(enumerateRiskSteps(narrow)).toEqual(['0.02', '0.03', '0.04'])
  })

  it('refuses degenerate policies instead of inventing positions', () => {
    expect(enumerateRiskSteps(dto({ step: '0' }))).toEqual([])
    expect(enumerateRiskSteps(dto({ min: '0.05', max: '0.01' }))).toEqual([])
    // Off-grid max: (max - min) is not a whole number of steps.
    expect(enumerateRiskSteps(dto({ max: '0.047' }))).toEqual([])
    expect(enumerateRiskSteps(dto({ min: 'abc' }))).toEqual([])
    // Absurd range can never build a giant strip.
    expect(enumerateRiskSteps(dto({ min: '0', max: '100', step: '0.005' }))).toEqual([])
  })

  it('every enumerated position passes the local policy gate', () => {
    const policy = dto()
    for (const step of enumerateRiskSteps(policy)) {
      expect(isRiskAllowed(policy, step)).toBe(true)
    }
  })
})

describe('nearestRiskStep (legacy off-grid value → advisory pre-select, exact integer math)', () => {
  it('rounds to the nearest grid step without float drift', () => {
    const policy = dto()
    expect(nearestRiskStep(policy, '0.012')).toBe('0.01')
    expect(nearestRiskStep(policy, '0.013')).toBe('0.015')
    expect(nearestRiskStep(policy, '0.0125')).toBe('0.015')
    expect(nearestRiskStep(policy, '0.03')).toBe('0.03')
  })

  it('clamps out-of-range values to the grid edges', () => {
    const policy = dto()
    expect(nearestRiskStep(policy, '0.001')).toBe('0.005')
    expect(nearestRiskStep(policy, '0.09')).toBe('0.05')
  })

  it('returns null on degenerate policy or garbage input', () => {
    expect(nearestRiskStep(dto({ step: '0' }), '0.012')).toBeNull()
    expect(nearestRiskStep(dto(), 'abc')).toBeNull()
  })
})

describe('formatRiskPercent', () => {
  it('renders server fractions as Russian percents', () => {
    expect(formatRiskPercent('0.01')).toBe('1,0%')
    expect(formatRiskPercent('0.02')).toBe('2,0%')
    expect(formatRiskPercent('0.015')).toBe('1,5%')
    expect(formatRiskPercent(null)).toBe('—')
    expect(formatRiskPercent('garbage')).toBe('—')
  })
})

describe('isRiskAllowed (local UX gate; backend re-validates as authority)', () => {
  it('accepts the grid edges and mid-grid steps', () => {
    const policy = dto()
    for (const fraction of ['0.005', '0.01', '0.015', '0.03', '0.05']) {
      expect(isRiskAllowed(policy, fraction)).toBe(true)
    }
  })

  it('rejects out-of-range and off-step fractions', () => {
    const policy = dto()
    for (const fraction of ['0.004', '0.001', '0.055', '0.06', '0.012', '0.007', '0.011', 'abc']) {
      expect(isRiskAllowed(policy, fraction)).toBe(false)
    }
  })

  it('derives everything from the DTO, never from a hardcoded policy', () => {
    const narrow = dto({ min: '0.02', max: '0.04', step: '0.01', presets: ['0.02', '0.03', '0.04'] })
    expect(isRiskAllowed(narrow, '0.01')).toBe(false)
    expect(isRiskAllowed(narrow, '0.03')).toBe(true)
    expect(isRiskAllowed(narrow, '0.05')).toBe(false)
  })
})

describe('isRiskDirty', () => {
  it('is clean only when confirmed and selected match', () => {
    expect(isRiskDirty('0.01', '0.01')).toBe(false)
    expect(isRiskDirty('0.01', '0.02')).toBe(true)
    expect(isRiskDirty('0.01', '0.015')).toBe(true)
  })
})

describe('demoLossUsdt (labelled demo, never a trade forecast)', () => {
  it('scales the demo balance by the selected fraction', () => {
    expect(RISK_DEMO_BALANCE_USDT).toBe(1240)
    expect(demoLossUsdt(1240, '0.02')).toContain('24,80')
    expect(demoLossUsdt(1240, '0.01')).toContain('12,40')
  })
})

describe('RiskRuler render', () => {
  const steps = enumerateRiskSteps(dto())

  it('renders one tick per grid position from props, never hardcoded', () => {
    const html = renderToStaticMarkup(<RiskRuler steps={steps} active="0.01" onPick={() => {}} />)
    expect(steps).toHaveLength(10)
    for (const label of ['0,5%', '1,0%', '1,5%', '2,0%', '2,5%', '3,0%', '3,5%', '4,0%', '4,5%', '5,0%']) {
      expect(html).toContain(label)
    }
    // A ruler, not a form: no manual input exists anymore.
    expect(html).not.toContain('<input')
  })

  it('marks exactly the active tick as selected', () => {
    const html = renderToStaticMarkup(<RiskRuler steps={steps} active="0.02" onPick={() => {}} />)
    expect(html).toContain('radiogroup')
    const checked = html.match(/aria-checked="true"/g) ?? []
    expect(checked).toHaveLength(1)
    expect(html).toContain('risk-tick-btn num active')
    // The previously confirmed 1% is no longer selected.
    const activeButton = html.match(/<button[^>]*aria-checked="true"[^>]*>.*?<\/button>/)?.[0] ?? ''
    expect(activeButton).toContain('2,0%')
  })

  it('exposes every position as a labelled radio for tap/keyboard handlers', () => {
    // Static markup cannot fire events; the contract is structural: each
    // position is a real labelled button carrying its fraction.
    const html = renderToStaticMarkup(<RiskRuler steps={steps} active="0.01" onPick={() => {}} />)
    expect(html.match(/data-fraction="0\.0\d5?"/g)).toHaveLength(10)
    expect(html.match(/role="radio"/g)).toHaveLength(10)
  })

  it('leaks no internal identifiers into the ruler markup', () => {
    const html = renderToStaticMarkup(<RiskRuler steps={steps} active="0.01" onPick={() => {}} />).toLowerCase()
    for (const forbidden of [
      'account_id',
      'command_key',
      'client_order_id',
      'private_key',
      'nonce',
      'payload',
      'max_leverage',
      'max_entry_deviation',
    ]) {
      expect(html).not.toContain(forbidden)
    }
  })
})

describe('RiskScreen render', () => {
  it('renders the loading frame with the heading and no strip before data arrives', () => {
    const html = renderToStaticMarkup(<RiskScreen auth={{ initData: 'test' }} />)
    expect(html).toContain('Риск на Stop')
    expect(html).toContain('Загрузка настроек риска')
    expect(html).not.toContain('risk-ruler')
    expect(html).not.toContain('risk-toast')
    expect(html).not.toContain('Сохранить')
  })

  it('leaks no internal identifiers into the loading markup', () => {
    const html = renderToStaticMarkup(<RiskScreen auth={{ initData: 'test' }} />).toLowerCase()
    for (const forbidden of [
      'account_id',
      'command_key',
      'client_order_id',
      'private_key',
      'nonce',
      'payload',
      'max_leverage',
      'max_entry_deviation',
    ]) {
      expect(html).not.toContain(forbidden)
    }
  })
})

describe('risk nested route (AppShell)', () => {
  it("renders RiskScreen for { kind: 'risk' }", () => {
    const html = renderToStaticMarkup(
      <NestedRoute nested={{ kind: 'risk' }} auth={{ initData: 'test' }} />,
    )
    expect(html).toContain('Риск на Stop')
  })
})
