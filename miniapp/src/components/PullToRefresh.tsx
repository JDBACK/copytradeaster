/** Pull-down at the top of the scroll surface = one explicit refresh.
 *
 * This replaces every manual "Обновить" affordance: nothing in the app asks a
 * user to re-read data, and when they do want to force a read they pull, the
 * way they already do in any exchange app. The gesture stays inside our own
 * scroller and only arms when the content is already at its top, so ordinary
 * scrolling and Telegram's sheet drag keep working everywhere else.
 */

import { useEffect, useRef, useState } from 'react'
import { PULL_IDLE, PULL_THRESHOLD_PX, createPullTracker, type PullState } from '../pullToRefresh'
import { hapticImpact, hapticNotify, setVerticalSwipes } from '../telegram'

/** How long the finished indicator stays before folding away. */
const SETTLE_MS = 320
/** Height the gap rests at while the refresh is in flight: enough for the
 *  spinner, not the whole distance the finger travelled. A phone-sized screen
 *  should not stay pushed down by a 90 px pull for a second. */
const REFRESH_REST_PX = 44

export function PullToRefresh({
  scroller,
  onRefresh,
}: {
  scroller: { current: HTMLElement | null }
  onRefresh: () => Promise<unknown>
}) {
  const [state, setState] = useState<PullState>(PULL_IDLE)
  const refresh = useRef(onRefresh)

  // Refs must not be written during render; the latest callback is picked up
  // after commit and read only from gesture handlers.
  useEffect(() => {
    refresh.current = onRefresh
  }, [onRefresh])

  // The shell carries the pull variables, not the indicator: the content rides
  // down with the finger and opens a real gap, and the indicator sits inside
  // that gap. Before this, a full-width band slid over the heading it had
  // nothing to do with, which read as an alien rectangle on the screen.
  useEffect(() => {
    const shell = scroller.current?.closest<HTMLElement>('.shell')
    if (!shell) return
    const dragging = state.phase === 'pulling' || state.phase === 'ready'
    const gap =
      state.phase === 'refreshing' ? Math.min(state.distance, REFRESH_REST_PX) : state.distance
    shell.style.setProperty('--pull-distance', `${gap}px`)
    shell.style.setProperty('--pull-progress', `${Math.min(1, state.distance / PULL_THRESHOLD_PX)}`)
    shell.style.setProperty('--pull-ease', dragging ? '0ms' : '260ms')
  }, [state, scroller])

  useEffect(() => {
    const element = scroller.current
    if (!element) return
    const tracker = createPullTracker()
    let busy = false
    let lastPhase = 'idle'
    // Telegram owns the vertical drag unless this gesture actually took it.
    let swipesHeld = false
    let settle: ReturnType<typeof setTimeout> | null = null

    const begin = (x: number, y: number) => {
      if (busy) return
      lastPhase = 'idle'
      const atTop = element.scrollTop <= 0
      tracker.start(x, y, atTop)
      // Telegram drags the whole mini app sheet down with the same gesture.
      // While the user is at the top of the content that gesture is ours; it is
      // handed straight back on release (Bot API 7.7+; older clients ignore it).
      if (atTop) {
        swipesHeld = true
        setVerticalSwipes(false)
      }
    }

    const releaseSwipes = () => {
      if (!swipesHeld) return
      swipesHeld = false
      setVerticalSwipes(true)
    }

    const move = (x: number, y: number, claim: (() => void) | null) => {
      const gesture = tracker.move(x, y)
      if (!gesture.owned) return
      claim?.()
      // One light tap the moment the pull becomes enough to refresh, the way a
      // native list tells the finger it has arrived. It replaces the words the
      // screen used to print over the content.
      if (gesture.phase === 'ready' && lastPhase !== 'ready') hapticImpact('light')
      lastPhase = gesture.phase
      setState({ phase: gesture.phase, distance: gesture.distance })
    }

    const finish = (cancelled: boolean) => {
      releaseSwipes()
      if (busy) return
      const { state: next, refresh: trigger } = tracker.release()
      if (cancelled || !trigger) {
        setState(cancelled ? tracker.cancel() : next)
        return
      }
      busy = true
      setState(next)
      hapticImpact('medium')
      void Promise.resolve(refresh.current())
        .then(
          () => hapticNotify('success'),
          () => hapticNotify('error'),
        )
        .finally(() => {
          busy = false
          settle = setTimeout(() => setState(PULL_IDLE), SETTLE_MS)
        })
    }

    const touchStart = (event: TouchEvent) => {
      if (event.touches.length !== 1) return
      const touch = event.touches[0]
      if (touch) begin(touch.clientX, touch.clientY)
    }
    const touchMove = (event: TouchEvent) => {
      const touch = event.touches[0]
      if (!touch) return
      move(touch.clientX, touch.clientY, () => {
        if (event.cancelable) event.preventDefault()
      })
    }
    const touchEnd = () => finish(false)
    const touchCancel = () => finish(true)

    const mouseMove = (event: MouseEvent) => {
      move(event.clientX, event.clientY, () => event.preventDefault())
    }
    const detachMouse = () => {
      window.removeEventListener('mousemove', mouseMove)
      window.removeEventListener('mouseup', mouseUp)
    }
    const mouseUp = () => {
      detachMouse()
      finish(false)
    }
    const mouseDown = (event: MouseEvent) => {
      if (event.button !== 0) return
      begin(event.clientX, event.clientY)
      window.addEventListener('mousemove', mouseMove)
      window.addEventListener('mouseup', mouseUp)
    }

    element.addEventListener('touchstart', touchStart, { passive: true })
    element.addEventListener('touchmove', touchMove, { passive: false })
    element.addEventListener('touchend', touchEnd)
    element.addEventListener('touchcancel', touchCancel)
    element.addEventListener('mousedown', mouseDown)
    return () => {
      element.removeEventListener('touchstart', touchStart)
      element.removeEventListener('touchmove', touchMove)
      element.removeEventListener('touchend', touchEnd)
      element.removeEventListener('touchcancel', touchCancel)
      element.removeEventListener('mousedown', mouseDown)
      detachMouse()
      if (settle) clearTimeout(settle)
      releaseSwipes()
    }
  }, [scroller])

  return (
    <div className="pull-refresh" data-phase={state.phase}>
      <span className="pull-refresh-spinner" aria-hidden>
        {/* One 270° arc that turns with the finger: the shape a native list
            shows, not a clock face standing in for a spinner. */}
        <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round">
          <path d="M12 3.4a8.6 8.6 0 1 0 8.6 8.6" />
        </svg>
      </span>
      {/* Spoken, not printed: the surface stays quiet while the spinner and
          the haptic tap carry the same news to the eye and the finger. */}
      <span className="visually-hidden" role="status" aria-live="polite">
        {state.phase === 'ready' ? 'Отпустите для обновления' : state.phase === 'refreshing' ? 'Обновляем…' : ''}
      </span>
    </div>
  )
}
