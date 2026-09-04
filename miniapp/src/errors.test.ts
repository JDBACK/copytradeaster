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
})
