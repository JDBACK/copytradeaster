/** Возраст вердикта остаётся честным, пока экран открыт: бэкенд отдаёт
 * age_seconds на момент ответа, и строка не должна замирать на этом числе. */

import { describe, expect, it } from 'vitest'
import { agedSeconds } from './useAge'

describe('agedSeconds', () => {
  it('прибавляет время, прошедшее с ответа', () => {
    expect(agedSeconds(10, 1_000_000, 1_030_000)).toBe(40)
    expect(agedSeconds(10, 1_000_000, 1_000_000)).toBe(10)
  })

  it('не стареет без известного времени ответа', () => {
    expect(agedSeconds(10, 0, 1_030_000)).toBe(10)
  })

  it('не выдумывает возраст из мусора', () => {
    expect(agedSeconds(null, 1_000_000, 1_030_000)).toBeNull()
    expect(agedSeconds(undefined, 1_000_000, 1_030_000)).toBeNull()
    expect(agedSeconds(-5, 1_000_000, 1_030_000)).toBeNull()
    expect(agedSeconds(Number.NaN, 1_000_000, 1_030_000)).toBeNull()
  })
})
