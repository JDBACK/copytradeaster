/** Russian-aware formatting helpers (UX plan §10: единые форматы, masked
 * addresses everywhere). */

export function formatFeeRate(value: string | number): string {
  const rate = typeof value === 'string' ? Number(value) : value
  if (!Number.isFinite(rate)) return String(value)
  const percent = rate * 100
  const digits = Number.isInteger(percent) ? 0 : 2
  return `до ${percent.toLocaleString('ru-RU', { maximumFractionDigits: digits })}%`
}

export function formatPercent(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const rate = typeof value === 'string' ? Number(value) : value
  if (!Number.isFinite(rate)) return '—'
  const percent = rate * 100
  return `${percent.toLocaleString('ru-RU', { maximumFractionDigits: 4 })}%`
}

export function formatUsdt(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const num = typeof value === 'string' ? Number(value) : value
  if (!Number.isFinite(num)) return '—'
  return `${num.toLocaleString('ru-RU', { maximumFractionDigits: 2 })} USDT`
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
}

export function formatDateFromMs(ms: number): string {
  if (!ms) return '—'
  return new Date(ms).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' })
}

export function countdownMinutes(targetMs: number, nowMs: number): number {
  return Math.max(0, Math.ceil((targetMs - nowMs) / 60_000))
}
