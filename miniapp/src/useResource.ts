import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from 'react'
import type { AuthContext } from './api'
import {
  MiniAppResources,
  RESOURCE_TTL_MS,
  type ReadStrength,
  type ResourceKey,
  type ResourceSnapshot,
  type ResourceValues,
} from './resources'

const RETRY_BACKOFF_MS = 1_000

/** Widening delay for self-healing reads; a real outage must never turn into a
 * REST storm, and a healthy screen never reaches this path at all. */
const SELF_HEAL_BACKOFF_MS = [1_000, 2_000, 5_000, 10_000, 20_000] as const

/** Test/standalone screen adapter. Production passes the App-owned instance. */
export function useScreenResources(
  provided: MiniAppResources | undefined,
  auth: AuthContext,
): MiniAppResources {
  return useMemo(
    () => provided ?? new MiniAppResources(auth, `standalone:${auth.devUserId ?? auth.initData}`),
    [provided, auth],
  )
}

export function useResource<K extends ResourceKey>(args: {
  resources: MiniAppResources
  key: K
  enabled?: boolean
  active?: boolean
  strength?: ReadStrength
  ttlMs?: number
  foregroundSignal?: number
}): {
  snapshot: ResourceSnapshot<ResourceValues[K]>
  refresh: (strength?: ReadStrength) => Promise<ResourceValues[K]>
} {
  const { resources, key } = args
  const subscribe = useCallback((listener: () => void) => resources.subscribe(key, listener), [resources, key])
  const getSnapshot = useCallback(() => resources.getSnapshot(key), [resources, key])
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  const enabled = args.enabled ?? true
  const active = args.active ?? true
  const strength = args.strength ?? 'weak'
  const ttlMs = args.ttlMs ?? RESOURCE_TTL_MS[key]
  const lastActive = useRef(active)
  const lastForegroundSignal = useRef(args.foregroundSignal ?? 0)
  const lastFailedRetryAt = useRef(0)

  useEffect(() => {
    const foregroundSignal = args.foregroundSignal ?? 0
    const reactivated = active && !lastActive.current
    const foregrounded = active && foregroundSignal !== lastForegroundSignal.current
    lastActive.current = active
    lastForegroundSignal.current = foregroundSignal
    if (!enabled) return
    const age = snapshot.receivedAt > 0 ? Date.now() - snapshot.receivedAt : Number.POSITIVE_INFINITY
    const strengthMissing = strength === 'strong' && snapshot.strength !== 'strong'
    // First warmup may run while hidden. Once any snapshot/error exists, stale
    // and TTL refreshes are active/foreground-only: hidden keep-alive != active
    // consumer. A failed read does not auto-loop; reactivation/foreground or an
    // explicit refresh is the retry edge.
    const needsInitial = snapshot.data === null && snapshot.error === null
    const retryEdge = snapshot.error !== null && (reactivated || foregrounded)
    const retryFailed = retryEdge && Date.now() - lastFailedRetryAt.current >= RETRY_BACKOFF_MS
    const needsActiveRefresh =
      active &&
      snapshot.data !== null &&
      snapshot.error === null &&
      (snapshot.stale || strengthMissing || age >= ttlMs)
    const needsRead = needsInitial || retryFailed || needsActiveRefresh
    if (!needsRead) return
    if (retryFailed) lastFailedRetryAt.current = Date.now()
    // Shared owner keeps the flight alive through StrictMode's synthetic
    // cleanup/setup. A second setup joins it instead of suppressing the retry.
    void resources.ensure(key, { strength, maxAgeMs: ttlMs }).catch(() => {})
  }, [
    resources,
    key,
    enabled,
    active,
    strength,
    ttlMs,
      args.foregroundSignal,
      snapshot.data,
      snapshot.error,
      snapshot.stale,
    snapshot.inflight,
    snapshot.receivedAt,
    snapshot.strength,
  ])

  const refresh = useCallback(
    (nextStrength: ReadStrength = strength) => resources.refresh(key, nextStrength),
    [resources, key, strength],
  )

  // Bounded self-healing. A failed read retries by itself while the screen is
  // on and in front of the user, which is what lets the whole app drop manual
  // refresh affordances: a screen that failed comes back without being asked.
  const failures = useRef(0)
  useEffect(() => {
    if (!enabled || !active) return
    if (snapshot.error === null) {
      failures.current = 0
      return
    }
    if (snapshot.inflight) return
    const delay = SELF_HEAL_BACKOFF_MS[Math.min(failures.current, SELF_HEAL_BACKOFF_MS.length - 1)]
    const timer = setTimeout(() => {
      failures.current += 1
      void resources.refresh(key, strength).catch(() => {})
    }, delay)
    return () => clearTimeout(timer)
  }, [resources, key, enabled, active, strength, snapshot.error, snapshot.inflight, snapshot.revision])

  return { snapshot, refresh }
}

/** Transport health as a user-facing verdict.
 *
 * ``snapshot.stale`` is a cache-revision flag: it is true while a live
 * invalidation is being re-read, which happens on every reconcile and on every
 * quote tick. Painting that as "data requires attention" made healthy screens
 * flash several times a minute, so routine background work read as a fault.
 * What a user needs to know is whether the connection works: a real request
 * failure, or the live channel's own debounced health (it only reports `stale`
 * after the stream has been down long enough that automatic recovery failed).
 * An in-flight refresh is not staleness and must never be shown as one.
 */
export function useTransportStale(resources: MiniAppResources, error: string | null): boolean {
  const live = useSyncExternalStore(
    resources.subscribeLive,
    resources.getLiveSnapshot,
    resources.getLiveSnapshot,
  )
  return error !== null || live.health === 'stale'
}
