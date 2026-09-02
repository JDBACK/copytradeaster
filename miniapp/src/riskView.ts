/** Pure Risk-screen helpers (эталон 05-risk-settings, P2.4).
 *
 * The UI works in percents ("1,5%") while the API travels in fraction
 * Decimals ("0.015").  All parsing/validation here is exact string math —
 * never float — but the backend remains the authority and re-validates
 * everything server-side.
 */

import type { RiskDTO } from './types'

/** Demo balance for the explicitly labelled demo calculation (USDT). */
export const RISK_DEMO_BALANCE_USDT = 1240

/** "0.02" → "0.02"; trims noise while keeping exactness. Null on garbage. */
export function normalizeFraction(raw: string): string | null {
  const text = raw.trim()
  if (!/^\d+(\.\d+)?$/.test(text)) return null
  const [head, tail = ''] = text.split('.')
  const digits = `${head}${tail}`
  if (!/^\d+$/.test(digits)) return null
  // Scale to units of 1e-5 without float math.
  if (tail.length > 5) {
    if (!/^0+$/.test(tail.slice(5))) return null
    const scaled = Number(head) * 100000 + Number(tail.slice(0, 5))
    if (!Number.isSafeInteger(scaled)) return null
    return scaledToFraction(scaled)
  }
  const scaled = Number(head) * 100000 + Number((tail + '00000').slice(0, 5))
  if (!Number.isSafeInteger(scaled)) return null
  return scaledToFraction(scaled)
}

function scaledToFraction(scaled: number): string {
  const head = Math.floor(scaled / 100000)
  const tail = String(scaled % 100000).padStart(5, '0')
  const trimmed = tail.replace(/0+$/, '')
  return trimmed ? `${head}.${trimmed}` : `${head}`
}

function fractionToScaled(fraction: string): number | null {
  const norm = normalizeFraction(fraction)
  if (norm === null) return null
  const [head, tail = ''] = norm.split('.')
  return Number(head) * 100000 + Number((tail + '00000').slice(0, 5))
}

/** Parse user-typed percents ("1,5", "1.5", "2%") into a fraction ("0.015").
 *  Returns null for anything that is not a plain non-negative decimal. */
export function parseRiskInput(raw: string): string | null {
  let text = raw.trim().replace(',', '.')
  if (text.endsWith('%')) text = text.slice(0, -1).trim()
  if (!/^\d+(\.\d+)?$/.test(text)) return null
  const [head, tail = ''] = text.split('.')
  if (tail.length > 3) {
    // Finer than milli-percent can never sit on the 0,5% policy grid.
    if (!/^0+$/.test(tail.slice(3))) return null
    return scaledToFraction(Number(head) * 1000 + Number(tail.slice(0, 3)))
  }
  const milli = Number(head) * 1000 + Number((tail + '000').slice(0, 3))
  if (!Number.isSafeInteger(milli)) return null
  // milli-percent == units of 1e-5 of the fraction (1% → 1000 → "0.01").
  return scaledToFraction(milli)
}

/** Fraction ("0.02") → Russian percent label ("2,0%"). Never throws. */
export function formatRiskPercent(fraction: string | null | undefined): string {
  if (fraction === null || fraction === undefined) return '—'
  const rate = Number(fraction)
  if (!Number.isFinite(rate)) return '—'
  const percent = rate * 100
  return `${percent.toLocaleString('ru-RU', { minimumFractionDigits: 1, maximumFractionDigits: 1 })}%`
}

/** Local UX check against the server-provided min/max/step.
 *  The backend re-validates as authority; this only drives disabled state. */
export function isRiskAllowed(dto: RiskDTO, fraction: string): boolean {
  const value = fractionToScaled(fraction)
  const min = fractionToScaled(dto.min)
  const max = fractionToScaled(dto.max)
  const step = fractionToScaled(dto.step)
  if (value === null || min === null || max === null || step === null) return false
  if (step <= 0 || value < min || value > max) return false
  return (value - min) % step === 0
}

/** Dirty check: the selected value differs from the server-confirmed one. */
export function isRiskDirty(confirmed: string, selected: string): boolean {
  const a = fractionToScaled(confirmed)
  const b = fractionToScaled(selected)
  if (a === null || b === null) return confirmed !== selected
  return a !== b
}

/** Demo loss for the labelled demo block: balance × fraction (display only,
 *  never presented as a forecast of a real trade). */
export function demoLossUsdt(balanceUsdt: number, fraction: string): string {
  const scaled = fractionToScaled(fraction)
  if (scaled === null) return '—'
  const loss = (balanceUsdt * scaled) / 100000
  return `${loss.toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} USDT`
}
