/** Pull-to-refresh: the app's only manual refresh affordance.
 *
 * Exchanges never ask a user to refresh — data heals itself. A pull down at the
 * top of a list is the one gesture everybody already knows, and it stays inside
 * the app's own scroll surface. The model below is pure so the thresholds and
 * damping are unit-testable without a DOM; ``components/PullToRefresh.tsx``
 * owns the event wiring.
 */

/** Travel at which releasing performs the refresh. */
export const PULL_THRESHOLD_PX = 64
/** A drag only becomes ours once it is clearly a vertical pull. */
export const PULL_SLOP_PX = 6
/** Hard ceiling for the indicator's travel; past the threshold the finger
 *  keeps moving but the sheet must not. */
export const PULL_MAX_PX = 96

export type PullPhase = 'idle' | 'pulling' | 'ready' | 'refreshing'

export interface PullState {
  phase: PullPhase
  distance: number
}

export const PULL_IDLE: PullState = { phase: 'idle', distance: 0 }

/** Damped travel for a raw downward delta. */
export function pullDistance(delta: number): number {
  if (!Number.isFinite(delta) || delta <= 0) return 0
  if (delta <= PULL_THRESHOLD_PX) return delta
  const overshoot = (delta - PULL_THRESHOLD_PX) * 0.34
  return Math.min(PULL_MAX_PX, PULL_THRESHOLD_PX + overshoot)
}

export function pullPhase(distance: number): PullPhase {
  return distance >= PULL_THRESHOLD_PX ? 'ready' : 'pulling'
}

export interface PullGesture {
  distance: number
  phase: PullPhase
  /** True once the drag passed the slop while the surface was at its top. */
  owned: boolean
}

export interface PullTracker {
  start(x: number, y: number, atTop: boolean): void
  move(x: number, y: number): PullGesture
  /** Terminal state for a released gesture plus whether it triggers a read. */
  release(): { state: PullState; refresh: boolean }
  cancel(): PullState
  current(): PullGesture
}

export function createPullTracker(): PullTracker {
  let startX = 0
  let startY = 0
  let armed = false
  let owned = false
  let distance = 0
  const current = (): PullGesture => ({
    distance,
    phase: owned && distance > 0 ? pullPhase(distance) : 'idle',
    owned,
  })
  const reset = (): PullState => {
    armed = false
    owned = false
    distance = 0
    return PULL_IDLE
  }
  return {
    start(x, y, atTop) {
      startX = x
      startY = y
      armed = atTop
      owned = false
      distance = 0
    },
    move(x, y) {
      if (!armed) return current()
      const delta = y - startY
      if (!owned) {
        // A diagonal drag still belongs to the page: only a clearly vertical
        // downward pull takes the gesture away from the scroller.
        if (delta <= PULL_SLOP_PX || Math.abs(x - startX) >= delta) return current()
        owned = true
      }
      distance = pullDistance(delta)
      return current()
    },
    release() {
      const refresh = owned && distance >= PULL_THRESHOLD_PX
      const state = refresh ? { phase: 'refreshing' as const, distance } : PULL_IDLE
      reset()
      return { state, refresh }
    },
    cancel() {
      return reset()
    },
    current,
  }
}
