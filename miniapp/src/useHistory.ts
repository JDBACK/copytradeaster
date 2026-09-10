import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from 'react'
import { historyKey, type HistoryQuery, type HistoryResources } from './historyResources'

/** Same bounded self-healing policy as screen resources. */
const SELF_HEAL_BACKOFF_MS = [1_000, 2_000, 5_000, 10_000, 20_000] as const

export function useHistory<Q extends HistoryQuery>(resources: HistoryResources, input: Q, active = true, foregroundSignal = 0) {
  const key = historyKey(input)
  // Query identity changes only with its values, never with the render object.
  const query = useMemo(() => JSON.parse(key) as Q, [key])
  const subscribe = useCallback((listener: () => void) => resources.subscribe(query, listener), [resources, query])
  const get = useCallback(() => resources.get(query), [resources, query])
  const snapshot = useSyncExternalStore(subscribe, get, get)
  useEffect(() => { if (active) void resources.load(query) }, [resources, query, active, foregroundSignal])
  const failures = useRef(0)
  useEffect(() => {
    if (!active) return
    if (snapshot.error === null) {
      failures.current = 0
      return
    }
    if (snapshot.loading) return
    const delay = SELF_HEAL_BACKOFF_MS[Math.min(failures.current, SELF_HEAL_BACKOFF_MS.length - 1)]
    const timer = setTimeout(() => {
      failures.current += 1
      void resources.retry(query).catch(() => {})
    }, delay)
    return () => clearTimeout(timer)
  }, [resources, query, active, snapshot.error, snapshot.loading, snapshot.receivedAt])
  return { ...snapshot, retry: () => resources.retry(query), refresh: () => resources.load(query, 'refresh'), more: () => resources.load(query, 'more'), fills: () => resources.load(query, 'fills'), events: () => resources.load(query, 'events') }
}
