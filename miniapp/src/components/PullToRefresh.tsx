/** Pull-down at the top of the scroll surface = one explicit refresh.
 *
 * This replaces every manual "Обновить" affordance: nothing in the app asks a
 * user to re-read data, and when they do want to force a read they pull, the
 * way they already do in any exchange app. The gesture stays inside our own
 * scroller and only arms when the content is already at its top, so ordinary
 * scrolling and Telegram's sheet drag keep working everywhere else.
 */

import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { PULL_IDLE, createPullTracker, type PullState } from '../pullToRefresh'
import { hapticImpact, hapticNotify, setVerticalSwipes } from '../telegram'

/** How long the finished indicator stays before folding away. */
const SETTLE_MS = 320

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

  useEffect(() => {
    const element = scroller.current
    if (!element) return
    const tracker = createPullTracker()
    let busy = false
    // Telegram owns the vertical drag unless this gesture actually took it.
    let swipesHeld = false
    let settle: ReturnType<typeof setTimeout> | null = null

    const begin = (x: number, y: number) => {
      if (busy) return
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

  const style = { '--pull-distance': `${state.distance}px` } as CSSProperties
  return (
    <div className="pull-refresh" data-phase={state.phase} style={style} aria-hidden={state.phase === 'idle'}>
      <span className="pull-refresh-icon" aria-hidden>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="8.5" />
          <path d="M12 8v5.5" />
          <path d="m9.6 11.3 2.4 2.4 2.4-2.4" />
        </svg>
      </span>
      {/* The gesture teaches itself: no instruction while pulling, one short
          confirmation at the moment releasing would actually refresh. */}
      <span className="pull-refresh-label" role="status" aria-live="polite">
        {state.phase === 'ready' ? 'Отпустите для обновления' : state.phase === 'refreshing' ? 'Обновляем…' : ''}
      </span>
    </div>
  )
}
