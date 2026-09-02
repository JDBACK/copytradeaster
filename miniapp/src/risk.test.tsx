/** Tests for the Risk on Stop screen (эталон 05-risk-settings, P2.4):
 * pure percent/fraction helpers over the backend-owned policy plus the
 * static render contract of the production screen. */

import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { NestedRoute } from './components/AppShell'
import { RiskScreen } from './screens/RiskScreen'
import {
  RISK_DEMO_BALANCE_USDT,
  demoLossUsdt,
  formatRiskPercent,
  isRiskAllowed,
  isRiskDirty,
  parseRiskInput,
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

describe('parseRiskInput (percent UI → fraction API, no float)', () => {
  it('accepts Russian comma, dot and an optional percent sign', () => {
    expect(parseRiskInput('1,5')).toBe('0.015')
    expect(parseRiskInput('1.5')).toBe('0.015')
    expect(parseRiskInput('2%')).toBe('0.02')
    expect(parseRiskInput(' 2,0 % ')).toBe('0.02')
    expect(parseRiskInput('1')).toBe('0.01')
    expect(parseRiskInput('5')).toBe('0.05')
    expect(parseRiskInput('0,5')).toBe('0.005')
  })

  it('rejects garbage without throwing', () => {
    for (const raw of ['', '   ', '%', 'abc', '1..5', '1,5,2', '-1', '1e2', 'NaN', 'Infinity']) {
      expect(parseRiskInput(raw)).toBeNull()
    }
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

describe('RiskScreen render', () => {
  it('renders the loading frame with the heading and no presets before data arrives', () => {
    const html = renderToStaticMarkup(<RiskScreen auth={{ initData: 'test' }} />)
    expect(html).toContain('Риск на Stop')
    expect(html).toContain('Загрузка настроек риска')
    expect(html).not.toContain('risk-preset')
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
