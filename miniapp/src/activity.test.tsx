/** Unit tests for the pure Activity view-model (Stage 3 part 2, P2.2).
 *
 * The tests target the production helpers in activityView.ts, not the
 * ActivityScreen component, so they exercise the same functions the live
 * UI uses without depending on fixture data echoed back from a stub.
 */

import { afterEach, describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { ActivityScreen } from './screens/ActivityScreen'
import {
  activityEmptyCopy,
  activityStatusLabel,
  activityStatusTone,
  dayBucket,
  groupActivityByDay,
  isActivityStatus,
} from './activityView'
import type { ActivityDayBucket } from './activityView'
import { formatTime } from './format'
import type { ActivityItemDTO } from './types'

function makeItem(overrides: Partial<ActivityItemDTO> = {}): ActivityItemDTO {
  return {
    category: 'trade',
    title: 'Открыта позиция BTCUSDT',
    status: 'completed',
    symbol: 'BTCUSDT',
    occurred_at: '2026-09-02T12:41:00+00:00',
    pending_age: null,
    remediation: null,
    ...overrides,
  }
}

describe('activityStatusLabel', () => {
  it('renders Russian copy for completed/pending/failed', () => {
    expect(activityStatusLabel(makeItem({ status: 'completed' }))).toBe('Выполнено')
    expect(activityStatusLabel(makeItem({ status: 'pending', pending_age: 7 }))).toBe(
      'Выполняется (7 сек)',
    )
    expect(activityStatusLabel(makeItem({ status: 'pending', pending_age: null }))).toBe(
      'Выполняется',
    )
    expect(activityStatusLabel(makeItem({ status: 'failed' }))).toBe('Ошибка')
  })
})

describe('activityStatusTone', () => {
  it('maps the three status values to CSS tones', () => {
    expect(activityStatusTone(makeItem({ status: 'completed' }))).toBe('success')
    expect(activityStatusTone(makeItem({ status: 'pending' }))).toBe('warning')
    expect(activityStatusTone(makeItem({ status: 'failed' }))).toBe('danger')
  })
})

describe('dayBucket', () => {
  it('classifies today, yesterday and earlier', () => {
    const now = new Date(2026, 8, 2, 13)
    expect(dayBucket(new Date(2026, 8, 2, 9).toISOString(), now)).toBe('today')
    expect(dayBucket(new Date(2026, 8, 1, 22).toISOString(), now)).toBe('yesterday')
    expect(dayBucket(new Date(2026, 7, 29, 10).toISOString(), now)).toBe('earlier')
    expect(dayBucket(null, now)).toBe('earlier')
    expect(dayBucket('not-a-date', now)).toBe('earlier')
  })
})

describe('groupActivityByDay', () => {
  it('orders groups today → yesterday → earlier and sorts items by occurred_at desc', () => {
    const now = new Date('2026-09-02T13:00:00+00:00')
    const items: ActivityItemDTO[] = [
      makeItem({ occurred_at: '2026-09-02T12:41:00+00:00' }),
      makeItem({ occurred_at: '2026-09-01T19:08:00+00:00' }),
      makeItem({ occurred_at: '2026-09-02T12:40:00+00:00' }),
      makeItem({ occurred_at: '2026-08-30T10:00:00+00:00' }),
    ]
    const groups = groupActivityByDay(items, now)
    expect(groups.map((g) => g.bucket)).toEqual(['today', 'yesterday', 'earlier'])
    expect(groups[0].items.map((i) => i.occurred_at)).toEqual([
      '2026-09-02T12:41:00+00:00',
      '2026-09-02T12:40:00+00:00',
    ])
    expect(groups[1].items).toHaveLength(1)
    expect(groups[2].items).toHaveLength(1)
  })
})

describe('activityEmptyCopy', () => {
  it('returns the right Russian copy per filter', () => {
    expect(activityEmptyCopy('all')).toBe('Действий пока нет')
    expect(activityEmptyCopy('trade')).toBe('Сделок пока нет')
    expect(activityEmptyCopy('system')).toBe('Системных событий пока нет')
  })
})

describe('isActivityStatus', () => {
  it('narrows the ActivityStatus type', () => {
    expect(isActivityStatus('completed')).toBe(true)
    expect(isActivityStatus('pending')).toBe(true)
    expect(isActivityStatus('failed')).toBe(true)
    expect(isActivityStatus('something-else')).toBe(false)
    expect(isActivityStatus(null)).toBe(false)
  })
})

describe('ActivityScreen render (read-only UI contract)', () => {
  it('renders the title, brand and loading state in the initial render', () => {
    const html = renderToStaticMarkup(
      <ActivityScreen auth={{ initData: 'test' }} />,
    )
    expect(html).toContain('Активность')
    expect(html).toContain('ASTER')
    expect(html).not.toContain('Aster testnet')
    expect(html).toContain('Загрузка журнала')
  })

  it('prefers the masked wallet chip over the testnet badge when an account is connected', () => {
    const html = renderToStaticMarkup(
      <ActivityScreen
        auth={{ initData: 'test' }}
        // oxlint-disable-next-line @typescript-eslint/no-explicit-any
        accountMasked={'0x1111…1111' as any}
      />,
    )
    expect(html).toContain('0x1111…1111')
  })

  it('does not invent the environment when no masked account is available', () => {
    const html = renderToStaticMarkup(
      <ActivityScreen auth={{ initData: 'test' }} />,
    )
    expect(html).not.toContain('Aster testnet')
  })
  it('contains no trading controls or raw journal codes (strictly read-only)', () => {
    const html = renderToStaticMarkup(
      <ActivityScreen auth={{ initData: 'test' }} />,
    )
    const lower = html.toLowerCase()
    for (const forbidden of [
      'close',
      'reverse',
      'buy',
      'sell',
      'закрыть',
      'перевернуть',
      'купить',
      'продать',
      'command_key',
      'client_order_id',
      'account_id',
      'private_key',
      'nonce',
      'payload',
      'traceback',
    ]) {
      expect(lower).not.toContain(forbidden)
    }
  })

  it('does not render Buy/Sell/Close action buttons in any state', () => {
    const html = renderToStaticMarkup(
      <ActivityScreen auth={{ initData: 'test' }} />,
    )
    // Initial actions select feed filters; none submits a trading mutation.
    expect(html.match(/role="tab"/g)?.length ?? 0).toBe(3)
    expect(html.match(/<button/g)?.length ?? 0).toBe(3)
  })
})

/** Fixed-zone matrix (UX plan §15, S3.4): the same instant may legally land in
 * different local days. Each case pins process.env.TZ itself (Node re-reads TZ
 * on every Date call), so the suite stays independent from the machine zone of
 * the developer or CI — this is the documented way TZ is set for these tests. */
describe('dayBucket fixed timezone matrix', () => {
  const originalTz = process.env.TZ

  afterEach(() => {
    if (originalTz === undefined) delete process.env.TZ
    else process.env.TZ = originalTz
  })

  type TzCase = {
    tz: 'UTC' | 'Europe/Moscow' | 'America/New_York'
    now: string
    iso: string
    bucket: ActivityDayBucket
    why: string
  }

  const cases: TzCase[] = [
    // One instant, three zones: 2026-09-01T22:30:00Z is Sep 1 in UTC/NY and
    // already Sep 2 in Moscow.
    { tz: 'UTC', now: '2026-09-02T13:00:00Z', iso: '2026-09-01T22:30:00Z', bucket: 'yesterday', why: 'UTC 22:30 Sep 1' },
    { tz: 'Europe/Moscow', now: '2026-09-02T13:00:00Z', iso: '2026-09-01T22:30:00Z', bucket: 'today', why: 'MSK 01:30 Sep 2' },
    { tz: 'America/New_York', now: '2026-09-02T13:00:00Z', iso: '2026-09-01T22:30:00Z', bucket: 'yesterday', why: 'EDT 18:30 Sep 1' },
    // Midnight boundary per zone.
    { tz: 'UTC', now: '2026-09-02T13:00:00Z', iso: '2026-09-01T23:59:59Z', bucket: 'yesterday', why: 'UTC 23:59:59' },
    { tz: 'UTC', now: '2026-09-02T13:00:00Z', iso: '2026-09-02T00:00:00Z', bucket: 'today', why: 'UTC 00:00:00' },
    { tz: 'Europe/Moscow', now: '2026-09-02T13:00:00Z', iso: '2026-09-01T20:59:59Z', bucket: 'yesterday', why: 'MSK 23:59:59' },
    { tz: 'Europe/Moscow', now: '2026-09-02T13:00:00Z', iso: '2026-09-01T21:00:00Z', bucket: 'today', why: 'MSK 00:00:00' },
    { tz: 'America/New_York', now: '2026-09-02T13:00:00Z', iso: '2026-09-02T03:59:59Z', bucket: 'yesterday', why: 'EDT 23:59:59' },
    { tz: 'America/New_York', now: '2026-09-02T13:00:00Z', iso: '2026-09-02T04:00:00Z', bucket: 'today', why: 'EDT 00:00:00' },
    // Month and year boundary: the local calendar day differs from the UTC day.
    { tz: 'UTC', now: '2026-10-01T00:30:00Z', iso: '2026-09-30T23:30:00Z', bucket: 'yesterday', why: 'UTC month boundary' },
    { tz: 'Europe/Moscow', now: '2026-10-01T12:00:00Z', iso: '2026-09-30T22:30:00Z', bucket: 'today', why: 'MSK 01:30 Oct 1' },
    { tz: 'Europe/Moscow', now: '2026-01-01T12:00:00Z', iso: '2025-12-31T22:00:00Z', bucket: 'today', why: 'MSK 01:00 Jan 1' },
    // DST in America/New_York: spring forward 2026-03-08 and fall back 2026-11-01.
    { tz: 'America/New_York', now: '2026-03-09T12:00:00Z', iso: '2026-03-09T03:59:59Z', bucket: 'yesterday', why: 'EDT 23:59:59 Mar 8' },
    { tz: 'America/New_York', now: '2026-03-09T12:00:00Z', iso: '2026-03-09T04:00:00Z', bucket: 'today', why: 'EDT 00:00:00 Mar 9' },
    { tz: 'America/New_York', now: '2026-03-09T12:00:00Z', iso: '2026-03-08T04:59:59Z', bucket: 'earlier', why: 'EST 23:59:59 Mar 7' },
    { tz: 'America/New_York', now: '2026-11-02T12:00:00Z', iso: '2026-11-02T04:59:59Z', bucket: 'yesterday', why: 'EST 23:59:59 Nov 1' },
    { tz: 'America/New_York', now: '2026-11-02T12:00:00Z', iso: '2026-11-02T05:00:00Z', bucket: 'today', why: 'EST 00:00:00 Nov 2' },
  ]

  for (const c of cases) {
    it(`${c.tz}: ${c.iso} at ${c.now} → ${c.bucket} (${c.why})`, () => {
      process.env.TZ = c.tz
      expect(dayBucket(c.iso, new Date(c.now))).toBe(c.bucket)
    })
  }

  it('keeps the displayed row time on the same local calendar as the bucket', () => {
    const now = new Date('2026-09-02T13:00:00Z')
    const iso = '2026-09-01T22:30:00Z'
    process.env.TZ = 'Europe/Moscow'
    expect(dayBucket(iso, now)).toBe('today')
    expect(formatTime(iso)).toBe('01:30')
    process.env.TZ = 'UTC'
    expect(dayBucket(iso, now)).toBe('yesterday')
    expect(formatTime(iso)).toBe('22:30')
  })
})
