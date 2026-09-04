/** Shared warm-activation primitive for the four root tabs.
 *
 * Reference pattern (first implemented in Home): `active` + `loadedAtRef` +
 * seq-guard + warm failure that never wipes ready UI. The effect fires only
 * on `active` transitions — never on `load` flips, so an intermediate
 * partial-ready state cannot spawn a self-sustaining refresh loop. Everything
 * except `active` is read through a mirror ref to keep the dependency list
 * at exactly `[active]`.
 *
 * Client floors never go below the server snapshot TTL
 * (`max(2 x reconcile, 30)` seconds, so >= 30 s).
 *
 * Cross-screen invalidation arrives as a monotonic `invalidateSignal`
 * counter owned by the shell: a newer signal marks the snapshot suspect.
 * It forces exactly one background refresh — immediately when active, on
 * the next activation otherwise — without skeleton or UI wipe. Signals
 * consumed while the first fetch is still in flight stay pending: the
 * in-flight frame predates the mutation, so the next activation refreshes.
 *
 * Single-flight: at most one automatic warm revalidation runs per screen.
 * A second activation while the previous background refresh is still in
 * flight is a no-op. `onRefresh` must return its promise so the flag
 * clears on settle; failure leaves the snapshot unfresh, so the next
 * suitable activation retries. No polling, no refresh loop: the stale flag
 * is cleared before `onRefresh` runs, so its setStates cannot retrigger.
 */

import { useCallback, useEffect, useRef } from 'react'

export const HOME_REVALIDATE_MS = 30_000
export const ACTIVITY_REVALIDATE_MS = 60_000

export type WarmReason = 'retry' | 'revalidate'

export function useWarmRevalidation(args: {
  /** True while this screen is the visible surface (tab selected, no nested over it). */
  active: boolean
  /** Client freshness floor in ms; never below the server snapshot TTL. */
  ttlMs: number
  /** True while the first fetch is still in flight — activation never fires then. */
  busy: boolean
  /** Shell-owned mutation counter; a newer value marks this snapshot stale. */
  invalidateSignal?: number
  /**
   * `retry` (never freshly loaded: rerun the foreground fetch, error UI on
   * failure) vs `revalidate` (stale ready snapshot: background refresh that
   * must not show a skeleton and must not wipe ready UI on failure).
   * Must return the refresh promise for single-flight tracking.
   */
  onRefresh: (reason: WarmReason) => void | Promise<unknown>
}): { markFresh: () => void } {
  const loadedAtRef = useRef(0)
  const freshRef = useRef(false)
  const seenSignalRef = useRef(0)
  const staleRef = useRef(false)
  const inflightRef = useRef(false)
  const liveRef = useRef(args)
  useEffect(() => {
    liveRef.current = args
  })

  /** Record a fully loaded snapshot; call on every successful load. */
  const markFresh = useCallback(() => {
    loadedAtRef.current = Date.now()
    freshRef.current = true
  }, [])

  useEffect(() => {
    const signal = args.invalidateSignal ?? 0
    if (signal !== seenSignalRef.current) {
      seenSignalRef.current = signal
      staleRef.current = true
    }
    if (!args.active) return
    const live = liveRef.current
    if (live.busy) return
    if (inflightRef.current) return
    if (!staleRef.current && Date.now() - loadedAtRef.current < live.ttlMs) return
    staleRef.current = false
    inflightRef.current = true
    let result: void | Promise<unknown>
    try {
      result = live.onRefresh(freshRef.current ? 'revalidate' : 'retry')
    } catch {
      inflightRef.current = false
      staleRef.current = true
      return
    }
    // Both handlers clear: the derived promise always settles fulfilled,
    // so a rejecting onRefresh can never surface as unhandled.
    Promise.resolve(result).then(
      () => {
        inflightRef.current = false
      },
      () => {
        inflightRef.current = false
      },
    )
  }, [args.active, args.invalidateSignal])

  return { markFresh }
}
