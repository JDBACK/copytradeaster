/** Global-gate error copy: a latched emergency or pause must read as an
 * operator decision, never as a generic outage. */

import { describe, expect, it } from 'vitest'
import { describeError } from './errors'

describe('describeError', () => {
  it('explains a latched global emergency as an operator safety latch', () => {
    const copy = describeError('global_emergency_active')
    expect(copy.title).toMatch(/глобальная защита/i)
    expect(copy.hint).toMatch(/оператор/i)
  })

  it('explains a closed global gate as an operator pause', () => {
    const copy = describeError('global_pause_active')
    expect(copy.title).toMatch(/приостановлены/i)
    expect(copy.hint).toMatch(/оператор/i)
  })

  it('keeps the generic outage copy for plain service_unavailable', () => {
    expect(describeError('service_unavailable').title).toBe('Сервис недоступен')
  })

  it('reads a not-ready engine as "wait a moment", never as "start over"', () => {
    const copy = describeError('not_ready')
    expect(copy.title).toMatch(/запуск/i)
    expect(copy.hint).toMatch(/секунд/i)
    expect(copy.hint).not.toMatch(/заново/i)
  })

  it('reads a dropped live transport as a degraded hint, not a failure', () => {
    const copy = describeError('live_unavailable')
    expect(copy.title).toMatch(/недоступны/i)
    expect(copy.hint).toMatch(/повтор/i)
  })
})
