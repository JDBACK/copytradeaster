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

const priceFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 20 })
const pnlFormat = new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const fullDateFormat = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZoneName: 'short' })

export function formatPrice(value: string | number | null | undefined): string {
  if (value == null) return '—'
  const num = Number(value)
  if (!Number.isFinite(num)) return '—'
  if (num !== 0 && Math.abs(num) < 1e-20) return `${num < 0 ? '−' : ''}< 0,00000000000000000001`
  return priceFormat.format(Object.is(num, -0) ? 0 : num)
}

export function formatQuantity(value: string | number | null | undefined, asset?: string | null): string {
  return `${formatPrice(value)} ${asset || '(единица не проверена)'}`
}

export function formatPnl(value: string | number | null | undefined): { text: string; tone: 'positive' | 'negative' | 'neutral' } {
  if (value == null || !Number.isFinite(Number(value))) return { text: '—', tone: 'neutral' }
  const num = Number(value)
  const prefix = num > 0 ? '+' : num < 0 ? '−' : ''
  const amount = num !== 0 && Math.abs(num) < 0.01 ? '< 0,01' : pnlFormat.format(Math.abs(num))
  return { text: `${prefix}${amount} USDT`, tone: num > 0 ? 'positive' : num < 0 ? 'negative' : 'neutral' }
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso || !Number.isFinite(Date.parse(iso))) return '—'
  return fullDateFormat.format(new Date(iso))
}

export function formatDuration(start: string | null, end: string | null, now = Date.now()): string {
  if (!start) return 'Начало уточняется'
  const duration = (end ? Date.parse(end) : now) - Date.parse(start)
  if (!Number.isFinite(duration) || duration < 0) return '—'
  const minutes = Math.floor(duration / 60000)
  if (minutes < 1) return 'менее минуты'
  if (minutes < 60) return `${minutes} мин`
  if (minutes < 1440) return `${Math.floor(minutes / 60)} ч ${minutes % 60} мин`
  return `${Math.floor(minutes / 1440)} дн ${Math.floor(minutes / 60) % 24} ч`
}

export function countdownMinutes(targetMs: number, nowMs: number): number {
  return Math.max(0, Math.ceil((targetMs - nowMs) / 60_000))
}
