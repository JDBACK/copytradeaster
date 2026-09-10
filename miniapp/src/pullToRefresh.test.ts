/** Pure pull-to-refresh model: thresholds, damping and gesture ownership. */

import { describe, expect, it } from 'vitest'
import {
  PULL_MAX_PX,
  PULL_SLOP_PX,
  PULL_THRESHOLD_PX,
  createPullTracker,
  pullDistance,
  pullPhase,
} from './pullToRefresh'

describe('pull-to-refresh model', () => {
  it('follows the finger one to one below the threshold', () => {
    expect(pullDistance(0)).toBe(0)
    expect(pullDistance(-40)).toBe(0)
    expect(pullDistance(Number.NaN)).toBe(0)
    expect(pullDistance(20)).toBe(20)
    expect(pullDistance(PULL_THRESHOLD_PX)).toBe(PULL_THRESHOLD_PX)
  })

  it('damps overshoot so a long pull never drags the sheet apart', () => {
    const damped = pullDistance(PULL_THRESHOLD_PX + 100)
    expect(damped).toBeGreaterThan(PULL_THRESHOLD_PX)
    expect(damped).toBeLessThan(PULL_THRESHOLD_PX + 100)
    expect(pullDistance(10_000)).toBe(PULL_MAX_PX)
  })

  it('reports ready only at the threshold', () => {
    expect(pullPhase(PULL_THRESHOLD_PX - 1)).toBe('pulling')
    expect(pullPhase(PULL_THRESHOLD_PX)).toBe('ready')
  })

  it('never takes a gesture that starts away from the top', () => {
    const tracker = createPullTracker()
    tracker.start(0, 100, false)
    expect(tracker.move(0, 260)).toEqual({ distance: 0, phase: 'idle', owned: false })
    expect(tracker.release()).toEqual({ state: { phase: 'idle', distance: 0 }, refresh: false })
  })

  it('ignores jitter and upward drags at the top', () => {
    const tracker = createPullTracker()
    tracker.start(10, 200, true)
    expect(tracker.move(10, 200 + PULL_SLOP_PX).owned).toBe(false)
    expect(tracker.move(10, 120).owned).toBe(false)
  })

  it('lets a diagonal drag scroll instead of refreshing', () => {
    const tracker = createPullTracker()
    tracker.start(0, 200, true)
    // 30 px down but 40 px sideways is a scroll, not a pull.
    expect(tracker.move(40, 230).owned).toBe(false)
  })

  it('owns a vertical pull and refreshes only past the threshold', () => {
    const short = createPullTracker()
    short.start(0, 200, true)
    short.move(2, 230)
    expect(short.release()).toEqual({ state: { phase: 'idle', distance: 0 }, refresh: false })

    const long = createPullTracker()
    long.start(0, 200, true)
    expect(long.move(2, 200 + PULL_THRESHOLD_PX + 10).phase).toBe('ready')
    const released = long.release()
    expect(released.refresh).toBe(true)
    expect(released.state.phase).toBe('refreshing')
  })

  it('a cancelled gesture never refreshes', () => {
    const tracker = createPullTracker()
    tracker.start(0, 0, true)
    tracker.move(0, PULL_THRESHOLD_PX + 20)
    expect(tracker.cancel()).toEqual({ phase: 'idle', distance: 0 })
    expect(tracker.release().refresh).toBe(false)
  })
})
