/** Behavioral tests for the RiskRuler control (S2.5 input lifecycle).
 *
 * Static markup cannot prove interaction: these tests mount the real
 * control in jsdom and drive the input paths the plan calls out —
 * gated/normalized wheel, mouse drag with capture semantics and scoped
 * click suppression, pointer cancel/lost-capture cleanup, interruption of
 * programmatic settle travel, and the quiet+stable settle fallback.
 * Geometry is synthetic: jsdom reports zero rects, so tests that need real
 * distances stub getBoundingClientRect/clientWidth/scrollWidth explicitly. */

// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { RiskRuler } from './components/RiskRuler'

// jsdom gaps the production code never meets in Telegram WebView.
if ((globalThis as { CSS?: unknown }).CSS === undefined) {
  ;(globalThis as { CSS?: unknown }).CSS = { escape: (value: string) => value }
}
if (typeof Element !== 'undefined' && typeof Element.prototype.scrollTo !== 'function') {
  Element.prototype.scrollTo = () => {}
}
if (typeof window !== 'undefined' && typeof window.requestAnimationFrame !== 'function') {
  window.requestAnimationFrame = (callback: FrameRequestCallback): number =>
    window.setTimeout(() => callback(performance.now()), 16) as unknown as number
  window.cancelAnimationFrame = (handle: number): void => window.clearTimeout(handle)
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const STEPS = ['0.005', '0.01', '0.015', '0.02', '0.03']

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

function renderRuler(onPick: (fraction: string, opts?: { silent?: boolean }) => void) {
  render(<RiskRuler steps={STEPS} active="0.01" onPick={onPick} />)
  return screen.getByRole('radiogroup')
}

/** Give the strip real scroll metrics: clientWidth 320, content 960. */
function stubMetrics(strip: HTMLElement): void {
  Object.defineProperty(strip, 'clientWidth', { value: 320, configurable: true })
  Object.defineProperty(strip, 'scrollWidth', { value: 960, configurable: true })
}

/** Model a real layout: tick i sits at i*64 when scrollLeft is 0 and rects
 *  shift with the strip's scroll position.  jsdom has no layout engine. */
function stubGeometry(strip: HTMLElement): void {
  stubMetrics(strip)
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
    if (this === strip) {
      return { left: 0, width: 320 } as DOMRect
    }
    const fraction = (this as HTMLElement).dataset?.fraction
    if (fraction !== undefined) {
      const index = STEPS.indexOf(fraction)
      return { left: index * 64 - strip.scrollLeft, width: 64 } as DOMRect
    }
    return { left: 0, width: 0 } as DOMRect
  })
}

describe('RiskRuler wheel (gated, normalized, boundary-aware)', () => {
  it('ignores vertical wheel over a non-activated control (page scrolls)', () => {
    const onPick = vi.fn()
    const strip = renderRuler(onPick)
    stubMetrics(strip)
    const prevented = !fireEvent.wheel(strip, { deltaY: 100, deltaX: 0 })
    expect(prevented).toBe(false)
    expect(strip.scrollLeft).toBe(0)
    expect(onPick).not.toHaveBeenCalled()
  })

  it('converts vertical wheel after intentional activation', async () => {
    const onPick = vi.fn()
    const strip = renderRuler(onPick)
    stubMetrics(strip)
    fireEvent.pointerDown(strip, { pointerType: 'touch', pointerId: 1 })
    const prevented = !fireEvent.wheel(strip, { deltaY: 100, deltaX: 0 })
    expect(prevented).toBe(true)
    await wait(50) // accumulated delta applies once per rAF
    expect(strip.scrollLeft).toBe(100)
  })

  it('normalizes line-mode deltas instead of jumping per notch', async () => {
    const strip = renderRuler(vi.fn())
    stubMetrics(strip)
    fireEvent.pointerDown(strip, { pointerType: 'touch', pointerId: 1 })
    fireEvent.wheel(strip, { deltaY: 2, deltaX: 0, deltaMode: 1 })
    await wait(50)
    expect(strip.scrollLeft).toBe(32) // 2 lines × 16px
  })

  it('never intercepts zoom (ctrl+wheel) or horizontal trackpad deltas', () => {
    const strip = renderRuler(vi.fn())
    stubMetrics(strip)
    fireEvent.pointerDown(strip, { pointerType: 'touch', pointerId: 1 })
    expect(fireEvent.wheel(strip, { deltaY: 100, ctrlKey: true })).toBe(true)
    expect(fireEvent.wheel(strip, { deltaY: 60, deltaX: 150 })).toBe(true)
    expect(strip.scrollLeft).toBe(0)
  })

  it('releases outward scroll at the boundary back to the page', async () => {
    const strip = renderRuler(vi.fn())
    stubMetrics(strip)
    fireEvent.pointerDown(strip, { pointerType: 'touch', pointerId: 1 })
    // Already at the left edge: scrolling further up-left is the page's.
    expect(fireEvent.wheel(strip, { deltaY: -80 })).toBe(true)
    expect(strip.scrollLeft).toBe(0)
    // Inward is consumed, then the right edge releases again.
    fireEvent.wheel(strip, { deltaY: 60 })
    await wait(50)
    expect(strip.scrollLeft).toBe(60)
    strip.scrollLeft = 640 // maxScroll = 960 - 320
    expect(fireEvent.wheel(strip, { deltaY: 80 })).toBe(true)
    expect(strip.scrollLeft).toBe(640)
  })

  it('deactivates when the next press lands outside the control', () => {
    const strip = renderRuler(vi.fn())
    stubMetrics(strip)
    fireEvent.pointerDown(strip, { pointerType: 'touch', pointerId: 1 })
    fireEvent.pointerDown(document.body)
    expect(fireEvent.wheel(strip, { deltaY: 100 })).toBe(true)
    expect(strip.scrollLeft).toBe(0)
  })
})

describe('RiskRuler mouse drag (capture semantics, scoped suppression)', () => {
  it('drags past the threshold, swallows only the terminating click', async () => {
    const onPick = vi.fn()
    const strip = renderRuler(onPick)
    fireEvent.pointerDown(strip, { pointerType: 'mouse', button: 0, clientX: 200, pointerId: 1 })
    fireEvent.pointerMove(strip, { pointerType: 'mouse', clientX: 150, pointerId: 1 })
    expect(strip.classList.contains('dragging')).toBe(true)
    await wait(40) // drag scrollLeft applies per rAF
    expect(strip.scrollLeft).toBe(50)
    fireEvent.pointerUp(strip, { pointerType: 'mouse', clientX: 150, pointerId: 1 })
    expect(strip.classList.contains('dragging')).toBe(false)
    // Release settles on the physically nearest step (zero geometry → first).
    expect(onPick).toHaveBeenCalledWith('0.005')

    onPick.mockClear()
    fireEvent.click(screen.getByRole('radio', { name: '3,0%' }))
    expect(onPick).not.toHaveBeenCalled() // terminating click eaten

    // A fresh independent click is never affected by the old suppression.
    fireEvent.pointerDown(strip, { pointerType: 'mouse', button: 0, clientX: 300, pointerId: 2 })
    fireEvent.pointerUp(strip, { pointerType: 'mouse', clientX: 300, pointerId: 2 })
    fireEvent.click(screen.getByRole('radio', { name: '3,0%' }))
    expect(onPick).toHaveBeenCalledWith('0.03')
  })

  it('press without movement stays a plain click (no suppression)', () => {
    const onPick = vi.fn()
    const strip = renderRuler(onPick)
    fireEvent.pointerDown(strip, { pointerType: 'mouse', button: 0, clientX: 200, pointerId: 1 })
    fireEvent.pointerUp(strip, { pointerType: 'mouse', clientX: 200, pointerId: 1 })
    fireEvent.click(screen.getByRole('radio', { name: '2,0%' }))
    expect(onPick).toHaveBeenCalledWith('0.02')
  })

  it('pointercancel mid-drag cleans up and never suppresses the next click', async () => {
    const onPick = vi.fn()
    const strip = renderRuler(onPick)
    fireEvent.pointerDown(strip, { pointerType: 'mouse', button: 0, clientX: 200, pointerId: 1 })
    fireEvent.pointerMove(strip, { pointerType: 'mouse', clientX: 150, pointerId: 1 })
    expect(strip.classList.contains('dragging')).toBe(true)
    fireEvent.pointerCancel(strip, { pointerType: 'mouse', pointerId: 1 })
    expect(strip.classList.contains('dragging')).toBe(false)
    await wait(40)
    onPick.mockClear()
    fireEvent.click(screen.getByRole('radio', { name: '1,5%' }))
    expect(onPick).toHaveBeenCalledWith('0.015')
  })
})

describe('RiskRuler programmatic settle travel (S3.4)', () => {
  it('tap fixes the draft at once, then the strip travels to the exact step', async () => {
    const onPick = vi.fn()
    const strip = renderRuler(onPick)
    stubGeometry(strip)
    fireEvent.click(screen.getByRole('radio', { name: '3,0%' }))
    // Intention is fixed synchronously — before any travel completes.
    expect(onPick).toHaveBeenCalledWith('0.03')
    expect(strip.classList.contains('animating')).toBe(true)
    onPick.mockClear()
    await wait(400)
    // tick 4 center 288 - mid 160 → target scrollLeft 128, ease-out landing.
    expect(strip.scrollLeft).toBe(128)
    expect(strip.classList.contains('animating')).toBe(false)
    // No passing steps were re-picked during programmatic travel, and the
    // settle afterwards is deduped against the tap acknowledgement.
    expect(onPick).not.toHaveBeenCalled()
  })

  it('a new gesture interrupts the animation from its physical position', async () => {
    const onPick = vi.fn()
    const strip = renderRuler(onPick)
    stubGeometry(strip)
    fireEvent.click(screen.getByRole('radio', { name: '3,0%' }))
    await wait(50) // a few frames in: mid-flight, not landed
    const midLeft = strip.scrollLeft
    expect(midLeft).toBeGreaterThan(0)
    expect(midLeft).toBeLessThan(128)
    fireEvent.pointerDown(strip, { pointerType: 'touch', pointerId: 1 })
    expect(strip.classList.contains('animating')).toBe(false)
    await wait(400)
    // Cancelled mid-flight: the strip keeps its physical spot, no jump to
    // the old target and no jump back to the start.
    expect(strip.scrollLeft).toBe(midLeft)
  })

  it('scroll frames during travel never re-pick passing steps', async () => {
    const onPick = vi.fn()
    const strip = renderRuler(onPick)
    stubGeometry(strip)
    fireEvent.click(screen.getByRole('radio', { name: '3,0%' }))
    onPick.mockClear()
    fireEvent.scroll(strip)
    await wait(50)
    expect(onPick).not.toHaveBeenCalled()
  })

  it('keyboard picks follow the same intentional path', () => {
    const onPick = vi.fn()
    const strip = renderRuler(onPick)
    stubGeometry(strip)
    fireEvent.keyDown(strip, { key: 'ArrowRight' })
    expect(onPick).toHaveBeenCalledWith('0.015')
    fireEvent.keyDown(strip, { key: 'Home' })
    expect(onPick).toHaveBeenCalledWith('0.005')
  })
})

describe('RiskRuler settle fallback (quiet window + stable frames)', () => {
  it('acknowledges the physically centered step once after scrolling stops', async () => {
    const onPick = vi.fn()
    const strip = renderRuler(onPick)
    // Zero jsdom geometry: every tick measures as the first one.
    fireEvent.pointerDown(strip, { pointerType: 'touch', pointerId: 1 })
    fireEvent.scroll(strip)
    await wait(40) // repaint frame: live silent tracking
    expect(onPick).toHaveBeenCalledWith('0.005', { silent: true })
    await wait(250) // 80ms quiet + 3 stable rAF frames → settle
    expect(onPick).toHaveBeenCalledWith('0.005')
  }, 10000)

  it('does not re-announce the step already acknowledged by a tap', async () => {
    const onPick = vi.fn()
    const strip = renderRuler(onPick)
    // Zero geometry: the tap target is the physically centered step already.
    fireEvent.click(screen.getByRole('radio', { name: '0,5%' }))
    expect(onPick).toHaveBeenCalledWith('0.005')
    onPick.mockClear()
    fireEvent.scroll(strip)
    await wait(300)
    // Silent live tracking may fire, but the settle never re-announces.
    const nonSilent = onPick.mock.calls.filter((call) => call[1]?.silent !== true)
    expect(nonSilent).toHaveLength(0)
  }, 10000)
})
