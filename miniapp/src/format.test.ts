import { describe, expect, it } from 'vitest'
import { countdownMinutes, formatFeeRate, formatPercent, formatPnl, formatPrice, formatTime, formatUsdt } from './format'

describe('russian formatting', () => {
  it('renders the builder fee cap as a percent with a decimal comma', () => {
    expect(formatFeeRate('0.0001')).toBe('до 0,01%')
    expect(formatFeeRate('0.001')).toBe('до 0,1%')
    expect(formatFeeRate(0.0005)).toBe('до 0,05%')
  })

  it('renders risk percent from fractional domain values', () => {
    expect(formatPercent('0.01')).toBe('1%')
    expect(formatPercent('0.015')).toBe('1,5%')
    expect(formatPercent('0.0001')).toBe('0,01%')
    expect(formatPercent(0)).toBe('0%')
    expect(formatPercent('1')).toBe('100%')
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

  it('formats prices and PnL correctly', () => {
    expect(formatPrice('60120.5')).toMatch(/60\s120,5/)
    expect(formatPrice(null)).toBe('—')

    expect(formatPnl('124.8')).toEqual({ text: '+124,80 USDT', tone: 'positive' })
    expect(formatPnl('-18.2')).toEqual({ text: '−18,20 USDT', tone: 'negative' })
    expect(formatPnl('0')).toEqual({ text: '0,00 USDT', tone: 'neutral' })
    expect(formatPnl(null)).toEqual({ text: '—', tone: 'neutral' })
  })

  it('preserves small amounts and prices instead of showing a false zero', () => {
    expect(formatPrice('0.000000123')).toBe('0,000000123')
    expect(formatPnl('0.001')).toEqual({ text: '+< 0,01 USDT', tone: 'positive' })
    expect(formatPnl('-0.001')).toEqual({ text: '−< 0,01 USDT', tone: 'negative' })
    expect(formatUsdt('0.001')).toBe('< 0,01 USDT')
    expect(formatUsdt('-0.001')).toBe('−< 0,01 USDT')
    expect(formatPnl('-0')).toEqual({ text: '0,00 USDT', tone: 'neutral' })
  })

  it('does not convert missing or malformed financial facts to zero', () => {
    for (const value of ['', '  ', 'NaN', 'Infinity', 'unknown']) {
      expect(formatPrice(value)).toBe('—')
      expect(formatUsdt(value)).toBe('—')
      expect(formatPnl(value)).toEqual({ text: '—', tone: 'neutral' })
    }
  })
})
