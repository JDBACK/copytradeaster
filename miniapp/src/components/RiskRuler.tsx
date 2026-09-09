/** Snap-strip risk ruler (S2.5 input lifecycle + S3.4 motion).
 *
 * Extracted from screens/RiskScreen.tsx: the control owns its whole
 * interaction lifecycle in one place — native touch/trackpad scrolling,
 * mouse drag with pointer capture, gated wheel conversion, keyboard,
 * programmatic settle animation and the freeze while a save is in flight.
 * The browser remains the only owner of touch inertia; JS handles
 * intentional picks and exact centering only.  Radio semantics are
 * preserved: one labelled button per policy step, valid by construction.
 *
 * Haptics (selection taps/crossings) fire through telegram.ts wrappers;
 * the selection itself never depends on them. */

import { useCallback, useEffect, useRef } from 'react'
import {
  DRAG_THRESHOLD_PX,
  SETTLE_QUIET_MS,
  SETTLE_STABLE_FRAMES,
  animationDurationMs,
  clampScroll,
  easeOutCubic,
  shouldSelectionHaptic,
  wheelIntent,
} from '../riskRuler'
import { formatRiskPercent } from '../riskView'
import { hapticImpact, hapticSelectionChanged } from '../telegram'

interface AnimatorState {
  generation: number
  raf: number
  fraction: string
}

interface DragState {
  pointerId: number
  startX: number
  startLeft: number
  moved: boolean
  captured: boolean
}

/** Exact edge padding so the first/last cell centers precisely under the
 *  fixed cursor.  Percentage CSS padding is unreliable here: it resolves
 *  against the wrapper while centering needs half of the ruler's own
 *  (bleed-widened) width — and WebKit does not re-snap programmatic
 *  scrolls the way Chromium does.  clientWidth is immune to all of that
 *  (padding never changes it), so compute in px on mount and on resize. */
function fitRulerPadding(strip: HTMLElement): void {
  const pad = Math.max(0, strip.clientWidth / 2 - 32)
  strip.style.paddingLeft = `${pad}px`
  strip.style.paddingRight = `${pad}px`
}

/** Paint the fish-eye: per-tick `--off` (0 at the strip center → 1 at the
 *  edge) drives tick scale/label fade in CSS.  Geometry is read as one
 *  batch, then written as one batch — interleaving reads/writes per tick
 *  would thrash layout every scroll frame.  Returns the centered fraction.
 *  Direct DOM writes under rAF — no React state per frame. */
function paintRuler(strip: HTMLElement): string | null {
  const stripRect = strip.getBoundingClientRect()
  const mid = stripRect.left + stripRect.width / 2
  const half = Math.max(stripRect.width / 2, 1)
  const nodes = strip.querySelectorAll('[data-fraction]')
  const reads: { el: HTMLElement; center: number }[] = []
  for (const node of nodes) {
    const el = node as HTMLElement
    const rect = el.getBoundingClientRect()
    reads.push({ el, center: rect.left + rect.width / 2 })
  }
  let best: string | null = null
  let bestDist = Number.POSITIVE_INFINITY
  for (const { el, center } of reads) {
    const dist = Math.abs(center - mid)
    el.style.setProperty('--off', Math.min(1, dist / half).toFixed(3))
    if (dist < bestDist) {
      bestDist = dist
      best = el.dataset.fraction ?? null
    }
  }
  return best
}

function prefersReducedMotion(): boolean {
  return (
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  )
}

/** Horizontal snap ruler: swipe / mouse drag / wheel / arrow keys.
 *  Pure: positions and the active value come from props, no fetching.
 *  One cell per policy step — a tick plus its label; the big value above
 *  follows the selection live while "Сейчас" keeps the confirmed truth. */
export function RiskRuler({
  steps,
  active,
  onPick,
  disabled = false,
}: {
  steps: string[]
  active: string
  onPick: (fraction: string, opts?: { silent?: boolean }) => void
  /** Frozen while a save is in flight: no re-pick can land mid-POST and get
   *  overwritten by the server response. */
  disabled?: boolean
}) {
  const stripRef = useRef<HTMLDivElement | null>(null)
  // Per-frame scroll repaint.
  const scrollRaf = useRef<number | null>(null)
  // centerImmediately's trailing correction pass.
  const centerRaf = useRef<number | null>(null)
  // Programmatic settle animation; null when no programmatic travel runs.
  const animator = useRef<AnimatorState | null>(null)
  // Monotonic motion generation: any new gesture/lock invalidates pending
  // animation callbacks scheduled by the previous one.
  const motionGen = useRef(0)
  // Vertical wheel conversion: accumulated px applied once per frame.
  const wheelAcc = useRef(0)
  const wheelRaf = useRef<number | null>(null)
  // Settle fallback for engines without reliable scrollend: quiet window,
  // then scrollLeft must hold still for a few consecutive frames.
  const settleTimer = useRef<number | null>(null)
  const settleRaf = useRef<number | null>(null)
  const settleLeft = useRef(0)
  const settleStable = useRef(0)
  // Mouse drag with pointer capture; touch never enters this path.
  const drag = useRef<DragState | null>(null)
  const dragRaf = useRef<number | null>(null)
  const pendingLeft = useRef(0)
  const blurHandler = useRef<(() => void) | null>(null)
  // Eats only the terminating click of a real drag — never a later one.
  const suppressClick = useRef(false)
  // Intentional activation (tap/click/keyboard focus) gates the wheel.
  const activated = useRef(false)
  // Latest freeze flag for async scroll/settle callbacks scheduled before
  // the lock: they must re-check it instead of applying a stale pick mid-POST.
  const disabledRef = useRef(disabled)
  // Previous freeze state: leaving the lock re-centers the active tick.
  const wasDisabled = useRef(disabled)
  // Live mirrors for event/rAF paths that must not close over stale renders.
  const activeRef = useRef(active)
  const onPickRef = useRef(onPick)
  // Last value acknowledged non-silently (tap or settle) — dedups settles.
  const announced = useRef(active)
  const lastStepHapticAt = useRef<number | null>(null)

  useEffect(() => {
    activeRef.current = active
  }, [active])
  useEffect(() => {
    onPickRef.current = onPick
  }, [onPick])

  const tickSelector = (fraction: string): string =>
    `[data-fraction="${CSS.escape(fraction)}"]`

  const cancelScrollWork = useCallback(() => {
    if (scrollRaf.current !== null) {
      cancelAnimationFrame(scrollRaf.current)
      scrollRaf.current = null
    }
    if (settleTimer.current !== null) {
      window.clearTimeout(settleTimer.current)
      settleTimer.current = null
    }
    if (settleRaf.current !== null) {
      cancelAnimationFrame(settleRaf.current)
      settleRaf.current = null
    }
  }, [])

  const stopAnimator = useCallback(() => {
    motionGen.current += 1
    if (animator.current !== null) {
      cancelAnimationFrame(animator.current.raf)
      animator.current = null
    }
    stripRef.current?.classList.remove('animating')
  }, [])

  /** Any new gesture or the save lock takes over from in-flight motion:
   *  animation, wheel accumulation, settle watchers and pending centering
   *  corrections are all cancelled; the strip stays at its physical spot. */
  const interruptMotion = useCallback(() => {
    stopAnimator()
    cancelScrollWork()
    if (centerRaf.current !== null) {
      cancelAnimationFrame(centerRaf.current)
      centerRaf.current = null
    }
    if (wheelRaf.current !== null) {
      cancelAnimationFrame(wheelRaf.current)
      wheelRaf.current = null
    }
    wheelAcc.current = 0
  }, [stopAnimator, cancelScrollWork])

  /** Immediate exact centering: mount/resize/save freeze/reduced-motion/
   *  repair.  Exact jump (not scrollIntoView smooth: smooth + mandatory
   *  snap stalls on multi-cell jumps) plus one correction pass — engines
   *  that re-snap programmatic scrolls (WebKit) can leave a residual. */
  const centerImmediately = useCallback((fraction: string) => {
    const strip = stripRef.current
    if (!strip) return
    const jump = (): number | null => {
      const el = strip.querySelector(tickSelector(fraction)) as HTMLElement | null
      if (!el) return null
      const stripRect = strip.getBoundingClientRect()
      const elRect = el.getBoundingClientRect()
      const delta = elRect.left + elRect.width / 2 - (stripRect.left + stripRect.width / 2)
      strip.scrollTo({ left: strip.scrollLeft + delta, behavior: 'auto' })
      return delta
    }
    jump()
    if (centerRaf.current !== null) cancelAnimationFrame(centerRaf.current)
    centerRaf.current = requestAnimationFrame(() => {
      centerRaf.current = null
      if (stripRef.current !== strip) return
      const residual = jump()
      if (residual !== null && Math.abs(residual) > 1) paintRuler(strip)
    })
  }, [])

  /** Programmatic settle travel for intentional picks (tap/keyboard/mouse
   *  drag release): snap is temporarily disabled (mandatory snap fights
   *  smooth travel in WebKit), scrollLeft moves monotonically ease-out,
   *  then snap returns and at most one correction pass runs.  Any new
   *  gesture cancels it by generation — never a jump back to the start. */
  const animateToStep = useCallback(
    (fraction: string) => {
      const strip = stripRef.current
      if (!strip) return
      if (prefersReducedMotion()) {
        centerImmediately(fraction)
        return
      }
      const el = strip.querySelector(tickSelector(fraction)) as HTMLElement | null
      if (!el) return
      const stripRect = strip.getBoundingClientRect()
      const elRect = el.getBoundingClientRect()
      const maxScroll = Math.max(0, strip.scrollWidth - strip.clientWidth)
      const from = strip.scrollLeft
      const target = clampScroll(
        from + elRect.left + elRect.width / 2 - (stripRect.left + stripRect.width / 2),
        0,
        maxScroll,
      )
      if (Math.abs(target - from) <= 1) {
        paintRuler(strip)
        return
      }
      stopAnimator()
      const generation = motionGen.current
      const duration = animationDurationMs(Math.abs(target - from))
      const start = performance.now()
      strip.classList.add('animating')
      const state: AnimatorState = { generation, raf: 0, fraction }
      animator.current = state
      const step = () => {
        if (animator.current !== state || motionGen.current !== generation) return
        // progress from our own clock: the rAF timestamp's origin is not
        // guaranteed to match performance.now() in every engine.
        const t = Math.min(1, (performance.now() - start) / duration)
        strip.scrollLeft = from + (target - from) * easeOutCubic(t)
        if (t < 1) {
          state.raf = requestAnimationFrame(step)
          return
        }
        animator.current = null
        strip.classList.remove('animating')
        if (centerRaf.current !== null) cancelAnimationFrame(centerRaf.current)
        centerRaf.current = requestAnimationFrame(() => {
          centerRaf.current = null
          if (motionGen.current !== generation || stripRef.current !== strip) return
          centerImmediately(fraction)
        })
      }
      state.raf = requestAnimationFrame(step)
    },
    [centerImmediately, stopAnimator],
  )

  /** End-of-gesture acknowledgement: report the physically centered step
   *  once (deduped against taps/travel) — never re-vibrates, never saves. */
  const finalizeSettle = useCallback(() => {
    const strip = stripRef.current
    if (!strip || disabledRef.current) return
    if (animator.current !== null || drag.current !== null) return
    const best = paintRuler(strip)
    if (best === null || best === announced.current) return
    announced.current = best
    onPickRef.current(best)
  }, [])

  /** Fallback settle detection; the native `scrollend` fast path (when the
   *  engine fires it) reaches the same deduped finalizeSettle, so arming
   *  both is safe and covers WebViews with unreliable scrollend. */
  const scheduleSettleFallback = useCallback(() => {
    if (settleTimer.current !== null) window.clearTimeout(settleTimer.current)
    settleTimer.current = window.setTimeout(() => {
      settleTimer.current = null
      const node = stripRef.current
      if (!node) return
      settleLeft.current = node.scrollLeft
      settleStable.current = 0
      const check = () => {
        settleRaf.current = null
        if (disabledRef.current || animator.current !== null || drag.current !== null) return
        const left = node.scrollLeft
        if (left === settleLeft.current) settleStable.current += 1
        else {
          settleLeft.current = left
          settleStable.current = 0
        }
        if (settleStable.current >= SETTLE_STABLE_FRAMES) finalizeSettle()
        else settleRaf.current = requestAnimationFrame(check)
      }
      settleRaf.current = requestAnimationFrame(check)
    }, SETTLE_QUIET_MS)
  }, [finalizeSettle])

  /** Mouse-drag finish/cancel: release capture, drop the dragging state and
   *  land on the nearest step — animated after a deliberate release,
   *  immediately after an abnormal cancel. */
  const settleAfterDrag = useCallback(
    (animate: boolean) => {
      const strip = stripRef.current
      if (!strip) return
      const best = paintRuler(strip)
      if (best === null) return
      if (best !== announced.current) {
        announced.current = best
        onPickRef.current(best)
      }
      if (animate) animateToStep(best)
      else centerImmediately(best)
    },
    [animateToStep, centerImmediately],
  )

  const endDragCleanup = useCallback(() => {
    const strip = stripRef.current
    const state = drag.current
    if (
      state?.captured &&
      strip &&
      typeof strip.releasePointerCapture === 'function'
    ) {
      try {
        strip.releasePointerCapture(state.pointerId)
      } catch {
        /* capture already lost */
      }
    }
    drag.current = null
    if (dragRaf.current !== null) {
      cancelAnimationFrame(dragRaf.current)
      dragRaf.current = null
    }
    strip?.classList.remove('dragging')
    if (blurHandler.current !== null) {
      window.removeEventListener('blur', blurHandler.current)
      blurHandler.current = null
    }
  }, [])

  /** Live tracking: every scroll frame repaints the fish-eye and moves the
   *  selection silently, so the big value follows the finger with no lag.
   *  Programmatic animation frames never re-pick passing steps and never
   *  vibrate — the target was fixed by the tap that started the travel. */
  const handleScroll = () => {
    if (disabledRef.current) return
    if (scrollRaf.current === null) {
      scrollRaf.current = requestAnimationFrame(() => {
        scrollRaf.current = null
        const strip = stripRef.current
        if (!strip || disabledRef.current) return
        const best = paintRuler(strip)
        if (animator.current !== null || best === null) return
        const rendered = strip
          .querySelector('[aria-checked="true"]')
          ?.getAttribute('data-fraction')
        if (best !== rendered) {
          onPickRef.current(best, { silent: true })
          const now = performance.now()
          if (shouldSelectionHaptic(lastStepHapticAt.current, now)) {
            lastStepHapticAt.current = now
            hapticSelectionChanged()
          }
        }
      })
    }
    scheduleSettleFallback()
  }

  /** Intentional pick (tap/click/arrow/Home/End): fixes the draft at once,
   *  then the strip travels to it.  One light impact only on a real change. */
  const tap = (fraction: string) => {
    if (disabledRef.current) return
    interruptMotion()
    suppressClick.current = false
    if (fraction !== activeRef.current) hapticImpact('light')
    announced.current = fraction
    onPickRef.current(fraction)
    animateToStep(fraction)
    // WebKit does not move focus to buttons on mouse/tap click: without
    // this, arrow keys stay dead until the next Tab.
    const strip = stripRef.current
    const el = strip?.querySelector(tickSelector(fraction)) as HTMLElement | null
    el?.focus({ preventScroll: true })
  }

  const onPointerDown = (event: React.PointerEvent) => {
    activated.current = true
    if (disabledRef.current) return
    // A new gesture interrupts programmatic travel from its physical spot.
    interruptMotion()
    suppressClick.current = false
    if (event.pointerType !== 'mouse' || event.button !== 0) return
    const strip = stripRef.current
    if (!strip) return
    drag.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startLeft: strip.scrollLeft,
      moved: false,
      captured: false,
    }
  }

  const onPointerMove = (event: React.PointerEvent) => {
    const state = drag.current
    const strip = stripRef.current
    if (!state || !strip || event.pointerId !== state.pointerId) return
    const dx = event.clientX - state.startX
    if (!state.moved) {
      if (Math.abs(dx) <= DRAG_THRESHOLD_PX) return
      state.moved = true
      // Capture keeps the gesture alive outside the strip bounds; snap is
      // disabled for the drag so mandatory snap cannot fight the pointer.
      if (typeof strip.setPointerCapture === 'function') {
        try {
          strip.setPointerCapture(event.pointerId)
          state.captured = true
        } catch {
          /* capture unsupported: drag still works inside the strip */
        }
      }
      strip.classList.add('dragging')
      const onWindowBlur = () => {
        if (drag.current === null) return
        endDragCleanup()
        settleAfterDrag(false)
      }
      blurHandler.current = onWindowBlur
      window.addEventListener('blur', onWindowBlur)
    }
    pendingLeft.current = state.startLeft - dx
    if (dragRaf.current === null) {
      dragRaf.current = requestAnimationFrame(() => {
        dragRaf.current = null
        const node = stripRef.current
        if (!node || drag.current === null) return
        node.scrollLeft = pendingLeft.current
      })
    }
  }

  const onPointerUp = (event: React.PointerEvent) => {
    const state = drag.current
    if (!state || event.pointerId !== state.pointerId) return
    const moved = state.moved
    endDragCleanup()
    if (!moved) return // a plain click proceeds to the tick handler
    // Swallow only the click that terminates this drag; a fresh pointerdown
    // already cleared the flag, so a later independent click is never eaten.
    suppressClick.current = true
    settleAfterDrag(true)
  }

  const onPointerCancel = (event: React.PointerEvent) => {
    const state = drag.current
    if (!state || event.pointerId !== state.pointerId) return
    endDragCleanup()
    settleAfterDrag(false)
  }

  const onLostPointerCapture = (event: React.PointerEvent) => {
    // Also fires after our own releasePointerCapture — drag is null then.
    const state = drag.current
    if (!state || event.pointerId !== state.pointerId) return
    endDragCleanup()
    settleAfterDrag(false)
  }

  const onBlur = (event: React.FocusEvent) => {
    const next = event.relatedTarget
    if (!(next instanceof Node) || !stripRef.current?.contains(next)) {
      activated.current = false
    }
  }

  const onClickCapture = (event: React.MouseEvent) => {
    if (suppressClick.current) {
      suppressClick.current = false
      event.stopPropagation()
      event.preventDefault()
    }
  }

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (disabledRef.current || steps.length === 0) return
    const index = steps.indexOf(active)
    let next = -1
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = Math.min(steps.length - 1, index + 1)
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = Math.max(0, index - 1)
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = steps.length - 1
    else return
    event.preventDefault()
    tap(steps[next])
  }

  // Freeze lifecycle: entering disabled cancels every pending scroll/settle/
  // animation/drag path and pins the exact value being saved; leaving it
  // re-centers the active tick — a native touch scroll may have visually
  // slipped the strip mid-POST while selection stayed frozen (handlers
  // ignore it, but the container is still a live scroll box until CSS locks
  // it — belt and braces).
  useEffect(() => {
    disabledRef.current = disabled
    if (disabled) {
      interruptMotion()
      if (drag.current !== null) endDragCleanup()
      centerImmediately(activeRef.current)
    } else if (wasDisabled.current) {
      centerImmediately(activeRef.current)
      const strip = stripRef.current
      if (strip) paintRuler(strip)
    }
    wasDisabled.current = disabled
  }, [disabled, interruptMotion, endDragCleanup, centerImmediately])

  // Center the confirmed position on mount and paint the first frame.
  // Padding is fitted first (px-exact, see fitRulerPadding) and refitted
  // on resize (rotation), keeping the checked tick under the cursor.
  useEffect(() => {
    const strip = stripRef.current
    if (!strip) return
    fitRulerPadding(strip)
    const checked = strip.querySelector('[aria-checked="true"]')?.getAttribute('data-fraction')
    if (checked) centerImmediately(checked)
    paintRuler(strip)
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => {
      fitRulerPadding(strip)
      const current = strip.querySelector('[aria-checked="true"]')?.getAttribute('data-fraction')
      if (current) centerImmediately(current)
      else paintRuler(strip)
    })
    observer.observe(strip)
    return () => observer.disconnect()
  }, [centerImmediately])

  // Native settle fast path where the engine provides it.
  useEffect(() => {
    const strip = stripRef.current
    if (!strip || !('onscrollend' in strip)) return
    const onScrollEnd = () => finalizeSettle()
    strip.addEventListener('scrollend', onScrollEnd)
    return () => strip.removeEventListener('scrollend', onScrollEnd)
  }, [finalizeSettle])

  // Gated vertical-wheel conversion (see wheelIntent): attached once,
  // decisions are pure and read live geometry per event.
  useEffect(() => {
    const strip = stripRef.current
    if (!strip) return
    const onWheel = (event: WheelEvent) => {
      if (disabledRef.current) return
      const maxScroll = Math.max(0, strip.scrollWidth - strip.clientWidth)
      const decision = wheelIntent({
        deltaX: event.deltaX,
        deltaY: event.deltaY,
        deltaMode: event.deltaMode,
        ctrlKey: event.ctrlKey,
        metaKey: event.metaKey,
        activated: activated.current,
        scrollLeft: strip.scrollLeft,
        maxScroll,
        pagePx: strip.clientWidth,
      })
      if (decision.action !== 'convert') return
      event.preventDefault()
      interruptMotion()
      wheelAcc.current += decision.px
      if (wheelRaf.current === null) {
        wheelRaf.current = requestAnimationFrame(() => {
          wheelRaf.current = null
          const node = stripRef.current
          const acc = wheelAcc.current
          wheelAcc.current = 0
          if (!node || disabledRef.current) return
          const limit = Math.max(0, node.scrollWidth - node.clientWidth)
          node.scrollLeft = clampScroll(node.scrollLeft + acc, 0, limit)
        })
      }
    }
    strip.addEventListener('wheel', onWheel, { passive: false })
    return () => strip.removeEventListener('wheel', onWheel)
  }, [interruptMotion])

  // Wheel activation ends when the next press lands outside the control.
  useEffect(() => {
    const deactivate = (event: Event) => {
      const strip = stripRef.current
      if (strip && event.target instanceof Node && !strip.contains(event.target)) {
        activated.current = false
      }
    }
    document.addEventListener('pointerdown', deactivate, true)
    return () => document.removeEventListener('pointerdown', deactivate, true)
  }, [])

  // Unmount: no rAF, timer, listener or capture may outlive the control.
  useEffect(
    () => () => {
      interruptMotion()
      endDragCleanup()
    },
    [interruptMotion, endDragCleanup],
  )

  return (
    <div className="risk-ruler-wrap">
      <div
        ref={stripRef}
        className="risk-ruler"
        role="radiogroup"
        aria-label="Выбор риска"
        aria-disabled={disabled || undefined}
        onScroll={handleScroll}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerCancel}
        onLostPointerCapture={onLostPointerCapture}
        onBlur={onBlur}
        onClickCapture={onClickCapture}
        onKeyDown={onKeyDown}
      >
        {steps.map((step) => (
          <button
            key={step}
            type="button"
            role="radio"
            aria-checked={step === active}
            aria-label={formatRiskPercent(step)}
            data-fraction={step}
            tabIndex={step === active ? 0 : -1}
            disabled={disabled}
            className={`risk-tick-btn num${step === active ? ' active' : ''}`}
            onClick={() => tap(step)}
          >
            <span className="risk-tick-mark" aria-hidden />
            <span className="risk-tick-label" aria-hidden>
              {formatRiskPercent(step)}
            </span>
          </button>
        ))}
      </div>
      <div className="risk-ruler-hint tone-muted">Тяните шкалу, чтобы выбрать значение</div>
      <div className="risk-ruler-cursor" aria-hidden />
    </div>
  )
}
