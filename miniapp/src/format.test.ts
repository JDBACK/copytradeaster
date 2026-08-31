import { describe, expect, it } from 'vitest'
import { countdownMinutes, formatFeeRate, formatPercent, formatTime, formatUsdt } from './format'

describe('russian formatting', () => {
  it('renders the builder fee cap as a percent with a decimal comma', () => {
    expect(formatFeeRate('0.0001')).toBe('до 0,01%')
    expect(formatFeeRate('0.001')).toBe('до 0,1%')
    expect(formatFeeRate(0.0005)).toBe('до 0,05%')
  })

  it('renders risk percent', () => {
    expect(formatPercent('1.5')).toBe('1,5%')
    expect(formatPercent(2)).toBe('2%')
    expect(formatPercent(null)).toBe('—')
    expect(formatPercent('garbage')).toBe('—')
  })

  it('renders USDT balance with ru grouping', () => {
    expect(formatUsdt(1000)).toMatch(/1\s000 USDT/)
    expect(formatUsdt('12.34')).toBe('12,34 USDT')
    expect(formatUsdt(null)).toBe('—')
  })

  it('renders time-of-day and dashes for missing values', () => {
    expect(formatTime(null)).toBe('—')
    expect(formatTime('2026-08-31T12:41:00+00:00')).toMatch(/^\d{2}:\d{2}$/)
  })

  it('counts approval window minutes without going negative', () => {
    const now = 1_700_000_000_000
    expect(countdownMinutes(now + 5.5 * 60_000, now)).toBe(6)
    expect(countdownMinutes(now - 60_000, now)).toBe(0)
  })
})
