/** Pure RiskRuler interaction helpers (S2.5/S3.4): wheel gating and
 *  normalization, settle animation easing/duration, scroll clamping and the
 *  selection-haptic rate limit.  No DOM, no React — decisions only. */

import { describe, expect, it } from 'vitest'
import {
  RULER_CELL_PX,
  SELECTION_HAPTIC_MIN_MS,
  animationDurationMs,
  clampScroll,
  easeOutCubic,
  normalizeWheelDelta,
  shouldSelectionHaptic,
  wheelIntent,
} from './riskRuler'

describe('clampScroll', () => {
  it('clamps to both edges', () => {
    expect(clampScroll(50, 0, 100)).toBe(50)
    expect(clampScroll(-5, 0, 100)).toBe(0)
    expect(clampScroll(150, 0, 100)).toBe(100)
  })
})

describe('easeOutCubic', () => {
  it('hits the exact endpoints and stays monotonic', () => {
    expect(easeOutCubic(0)).toBe(0)
    expect(easeOutCubic(1)).toBe(1)
    let prev = 0
    for (let i = 1; i <= 20; i++) {
      const value = easeOutCubic(i / 20)
      expect(value).toBeGreaterThan(prev)
      prev = value
    }
  })

  it('clamps out-of-range input instead of overshooting', () => {
    expect(easeOutCubic(-0.5)).toBe(0)
    expect(easeOutCubic(1.5)).toBe(1)
  })
})

describe('animationDurationMs (140–220 ms by distance)', () => {
  it('is fastest for a single step and caps long jumps', () => {
    expect(animationDurationMs(0)).toBe(140)
    expect(animationDurationMs(RULER_CELL_PX)).toBe(160)
    expect(animationDurationMs(RULER_CELL_PX * 4)).toBe(220)
    expect(animationDurationMs(RULER_CELL_PX * 12)).toBe(220)
  })

  it('grows monotonically with distance', () => {
    expect(animationDurationMs(RULER_CELL_PX * 2)).toBeGreaterThan(
      animationDurationMs(RULER_CELL_PX),
    )
  })
})

describe('shouldSelectionHaptic (rate limit, no queue)', () => {
  it('allows the first crossing, then throttles', () => {
    expect(shouldSelectionHaptic(null, 1000)).toBe(true)
    expect(shouldSelectionHaptic(1000, 1000 + SELECTION_HAPTIC_MIN_MS - 1)).toBe(false)
    expect(shouldSelectionHaptic(1000, 1000 + SELECTION_HAPTIC_MIN_MS)).toBe(true)
  })
})

describe('normalizeWheelDelta (deltaMode → px)', () => {
  it('passes pixel deltas through', () => {
    expect(normalizeWheelDelta(42, 0)).toBe(42)
    expect(normalizeWheelDelta(-7.5, 0)).toBe(-7.5)
  })

  it('scales line and page modes instead of jumping per unit', () => {
    expect(normalizeWheelDelta(3, 1)).toBe(48)
    expect(normalizeWheelDelta(1, 2)).toBe(480)
    expect(normalizeWheelDelta(1, 2, 16, 300)).toBe(300)
  })
})

describe('wheelIntent (§21.3 gating)', () => {
  const base = {
    deltaX: 0,
    deltaY: 100,
    deltaMode: 0,
    ctrlKey: false,
    metaKey: false,
    activated: true,
    scrollLeft: 50,
    maxScroll: 500,
  }

  it('converts an activated vertical wheel into ruler travel', () => {
    expect(wheelIntent(base)).toEqual({ action: 'convert', px: 100 })
  })

  it('never intercepts plain hover: activation is required', () => {
    expect(wheelIntent({ ...base, activated: false })).toEqual({ action: 'native' })
  })

  it('leaves horizontal-dominant trackpad deltas fully native', () => {
    expect(wheelIntent({ ...base, deltaX: 150 })).toEqual({ action: 'native' })
    expect(wheelIntent({ ...base, deltaX: -100 })).toEqual({ action: 'native' }) // equal = not dominant-Y
    expect(wheelIntent({ ...base, deltaY: 0, deltaX: 0 })).toEqual({ action: 'native' })
  })

  it('never touches zoom gestures (ctrl/cmd + wheel)', () => {
    expect(wheelIntent({ ...base, ctrlKey: true })).toEqual({ action: 'native' })
    expect(wheelIntent({ ...base, metaKey: true })).toEqual({ action: 'native' })
  })

  it('normalizes line mode before converting', () => {
    expect(wheelIntent({ ...base, deltaY: 2, deltaMode: 1 })).toEqual({ action: 'convert', px: 32 })
  })

  it('releases outward scroll at the boundaries back to the page', () => {
    expect(wheelIntent({ ...base, deltaY: -80, scrollLeft: 0 })).toEqual({ action: 'release' })
    expect(wheelIntent({ ...base, deltaY: 80, scrollLeft: 500 })).toEqual({ action: 'release' })
    // Inward from a boundary is still consumed.
    expect(wheelIntent({ ...base, deltaY: 80, scrollLeft: 0 })).toEqual({ action: 'convert', px: 80 })
    expect(wheelIntent({ ...base, deltaY: -80, scrollLeft: 500, maxScroll: 500 })).toEqual({
      action: 'convert',
      px: -80,
    })
  })
})
