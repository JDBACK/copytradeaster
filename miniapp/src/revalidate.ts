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
  /**
   * `retry` (never freshly loaded: rerun the foreground fetch, error UI on
   * failure) vs `revalidate` (stale ready snapshot: background refresh that
   * must not show a skeleton and must not wipe ready UI on failure).
   */
  onRefresh: (reason: WarmReason) => void
}): { markFresh: () => void } {
  const loadedAtRef = useRef(0)
  const freshRef = useRef(false)
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
    if (!args.active) return
    const live = liveRef.current
    if (live.busy) return
    if (Date.now() - loadedAtRef.current < live.ttlMs) return
    live.onRefresh(freshRef.current ? 'revalidate' : 'retry')
  }, [args.active])

  return { markFresh }
}
