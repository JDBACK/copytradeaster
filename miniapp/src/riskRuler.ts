/** Pure RiskRuler interaction helpers (S2.5 lifecycle / S3.4 motion).
 *
 * Everything here is DOM-free and unit-testable in node: wheel delta
 * normalization and gating, programmatic settle easing/duration, scroll
 * clamping and the selection-haptic rate limit.  The component owns the
 * refs; these functions own the decisions. */

/** Width of one ruler cell in px; must match `.risk-tick-btn` in CSS. */
export const RULER_CELL_PX = 64

/** Mouse travel before a press becomes a drag (px). */
export const DRAG_THRESHOLD_PX = 6

/** Settle fallback: scroll quiet for this long, then scrollLeft must stay
 *  stable for a few consecutive frames before the value is acknowledged. */
export const SETTLE_QUIET_MS = 80
export const SETTLE_STABLE_FRAMES = 3

/** Selection haptics: at most one pulse per window, never a backlog. */
export const SELECTION_HAPTIC_MIN_MS = 50

/** Wheel line/page modes normalized with these defaults (px). */
export const WHEEL_LINE_PX = 16
export const WHEEL_PAGE_PX = 480

/** Sub-pixel slack when comparing against scroll boundaries. */
const EDGE_EPS_PX = 1

export function clampScroll(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

/** Ease-out cubic, clamped to [0, 1]: fast start, soft exact landing. */
export function easeOutCubic(t: number): number {
  const u = 1 - Math.min(1, Math.max(0, t))
  return 1 - u * u * u
}

/** Programmatic settle duration by travel distance: 140–220 ms.
 *  One cell lands quickly, long jumps stay readable. */
export function animationDurationMs(distancePx: number): number {
  const ratio = Math.min(1, Math.abs(distancePx) / (RULER_CELL_PX * 4))
  return Math.round(140 + 80 * ratio)
}

/** True when a step crossing may vibrate: first crossing always passes,
 *  then at most one pulse per SELECTION_HAPTIC_MIN_MS. */
export function shouldSelectionHaptic(lastAt: number | null, now: number): boolean {
  return lastAt === null || now - lastAt >= SELECTION_HAPTIC_MIN_MS
}

/** Normalize a wheel delta to CSS pixels.
 *  deltaMode: 0 = pixels, 1 = lines, 2 = pages. */
export function normalizeWheelDelta(
  delta: number,
  deltaMode: number,
  linePx = WHEEL_LINE_PX,
  pagePx = WHEEL_PAGE_PX,
): number {
  if (deltaMode === 1) return delta * linePx
  if (deltaMode === 2) return delta * pagePx
  return delta
}

export type WheelDecision =
  | { action: 'native' } // browser keeps it: page scroll, trackpad inertia, zoom
  | { action: 'release' } // outward at a boundary: hand the page the scroll
  | { action: 'convert'; px: number } // vertical wheel drives the ruler

/** Vertical-wheel gating (§21.3): convert to ruler travel only after the
 *  control was intentionally activated (tap/click/keyboard focus) and the
 *  strip can actually consume that direction.  Horizontal-dominant deltas
 *  (trackpad) and zoom gestures are never intercepted; at the edges the
 *  outward component is released back to the page.  No UA sniffing:
 *  direction, deltaMode and activation describe the intent. */
export function wheelIntent(input: {
  deltaX: number
  deltaY: number
  deltaMode: number
  ctrlKey: boolean
  metaKey: boolean
  activated: boolean
  scrollLeft: number
  maxScroll: number
  linePx?: number
  pagePx?: number
}): WheelDecision {
  if (input.ctrlKey || input.metaKey) return { action: 'native' }
  if (input.deltaY === 0 || Math.abs(input.deltaX) >= Math.abs(input.deltaY)) {
    return { action: 'native' }
  }
  if (!input.activated) return { action: 'native' }
  const px = normalizeWheelDelta(
    input.deltaY,
    input.deltaMode,
    input.linePx ?? WHEEL_LINE_PX,
    input.pagePx ?? WHEEL_PAGE_PX,
  )
  const outwardLow = px < 0 && input.scrollLeft <= EDGE_EPS_PX
  const outwardHigh = px > 0 && input.scrollLeft >= input.maxScroll - EDGE_EPS_PX
  if (outwardLow || outwardHigh) return { action: 'release' }
  return { action: 'convert', px }
}
