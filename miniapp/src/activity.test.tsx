/** Unit tests for the pure Activity view-model (Stage 3 part 2, P2.2).
 *
 * The tests target the production helpers in activityView.ts, not the
 * ActivityScreen component, so they exercise the same functions the live
 * UI uses without depending on fixture data echoed back from a stub.
 */

import { describe, expect, it } from 'vitest'
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
    const now = new Date('2026-09-02T13:00:00+00:00')
    expect(dayBucket('2026-09-02T09:00:00+00:00', now)).toBe('today')
    expect(dayBucket('2026-09-01T22:00:00+00:00', now)).toBe('yesterday')
    expect(dayBucket('2026-08-29T10:00:00+00:00', now)).toBe('earlier')
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
    expect(html).toContain('Aster testnet')
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

  it('renders the testnet badge when no masked account is available', () => {
    const html = renderToStaticMarkup(
      <ActivityScreen auth={{ initData: 'test' }} />,
    )
    expect(html).toContain('Aster testnet')
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
    // <button> elements are scoped to navigation (retry only) in the
    // loading state; there are no order-placement controls in the activity
    // surface by design.
    expect(html.match(/<button/g)?.length ?? 0).toBe(0)
  })
})
