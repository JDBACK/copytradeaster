/** Pure Activity view-model helpers (Stage 3 part 2, P2.2).
 *
 * Kept out of ActivityScreen.tsx so the screen file only exports components
 * (required for fast refresh) and so unit tests can target the view-model
 * directly without rendering React.
 */

import type { ActivityFilter, ActivityItemDTO, ActivityStatus } from './types'

export const ACTIVITY_TIME_LABELS = {
  today: 'Сегодня',
  yesterday: 'Вчера',
  earlier: 'Ранее',
} as const

export type ActivityDayBucket = keyof typeof ACTIVITY_TIME_LABELS

export function dayBucket(iso: string | null | undefined, now: Date): ActivityDayBucket {
  if (!iso) return 'earlier'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return 'earlier'
  // Always bucket in UTC so the same instant groups identically across
  // timezones — important because the journal timestamps are UTC ISO.
  const todayKey = `${now.getUTCFullYear()}-${now.getUTCMonth()}-${now.getUTCDate()}`
  const dayKey = `${date.getUTCFullYear()}-${date.getUTCMonth()}-${date.getUTCDate()}`
  if (dayKey === todayKey) return 'today'
  const yesterday = new Date(now)
  yesterday.setUTCDate(now.getUTCDate() - 1)
  const yesterdayKey = `${yesterday.getUTCFullYear()}-${yesterday.getUTCMonth()}-${yesterday.getUTCDate()}`
  if (dayKey === yesterdayKey) return 'yesterday'
  return 'earlier'
}

export function activityStatusLabel(item: ActivityItemDTO): string {
  if (item.status === 'completed') return 'Выполнено'
  if (item.status === 'pending') {
    if (item.pending_age !== null && item.pending_age !== undefined) {
      return `Выполняется (${item.pending_age} сек)`
    }
    return 'Выполняется'
  }
  return 'Ошибка'
}

export function activityStatusTone(
  item: ActivityItemDTO,
): 'success' | 'warning' | 'danger' {
  if (item.status === 'completed') return 'success'
  if (item.status === 'pending') return 'warning'
  return 'danger'
}

export function activityEmptyCopy(filter: ActivityFilter): string {
  if (filter === 'trade') return 'Сделок пока нет'
  if (filter === 'system') return 'Системных событий пока нет'
  return 'Действий пока нет'
}

export function groupActivityByDay(
  items: ActivityItemDTO[],
  now: Date,
): Array<{ bucket: ActivityDayBucket; items: ActivityItemDTO[] }> {
  const order: ActivityDayBucket[] = ['today', 'yesterday', 'earlier']
  const buckets = new Map<ActivityDayBucket, ActivityItemDTO[]>()
  for (const bucket of order) buckets.set(bucket, [])
  for (const item of items) {
    const bucket = dayBucket(item.occurred_at, now)
    buckets.get(bucket)?.push(item)
  }
  for (const list of buckets.values()) {
    list.sort((left, right) => {
      const a = new Date(left.occurred_at ?? 0).getTime()
      const b = new Date(right.occurred_at ?? 0).getTime()
      return b - a
    })
  }
  return order
    .map((bucket) => ({ bucket, items: buckets.get(bucket) ?? [] }))
    .filter((group) => group.items.length > 0)
}

export function isActivityStatus(value: unknown): value is ActivityStatus {
  return value === 'completed' || value === 'pending' || value === 'failed'
}
