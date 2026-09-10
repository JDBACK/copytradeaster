/** Age of a server verdict, kept honest while the screen stays open.
 *
 * The backend sends ``age_seconds`` as of the response, so a row left on screen
 * would keep saying "15 сек назад" for as long as the user looks at it. This
 * adds the time elapsed since that response, on a slow tick: the label stays
 * truthful: the number moves like a clock, and nothing contradicts itself when it does. */

import { useEffect, useState } from 'react'

const TICK_MS = 15_000

export function useAgeSeconds(
  baseSeconds: number | null | undefined,
  receivedAt: number,
): number | null {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), TICK_MS)
    return () => clearInterval(timer)
  }, [])
  return agedSeconds(baseSeconds, receivedAt, now)
}

/** Pure part of the hook, so the arithmetic is unit-testable without a timer. */
export function agedSeconds(
  baseSeconds: number | null | undefined,
  receivedAt: number,
  now: number,
): number | null {
  if (typeof baseSeconds !== 'number' || !Number.isFinite(baseSeconds) || baseSeconds < 0) {
    return null
  }
  if (!receivedAt) return baseSeconds
  return baseSeconds + Math.max(0, (now - receivedAt) / 1000)
}
