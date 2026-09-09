import { useCallback, useEffect, useMemo, useSyncExternalStore } from 'react'
import { historyKey, type HistoryQuery, type HistoryResources } from './historyResources'
export function useHistory<Q extends HistoryQuery>(resources: HistoryResources, input: Q, active = true, foregroundSignal = 0) {
  const key = historyKey(input)
  // Query identity changes only with its values, never with the render object.
  const query = useMemo(() => JSON.parse(key) as Q, [key])
  const subscribe = useCallback((listener: () => void) => resources.subscribe(query, listener), [resources, query])
  const get = useCallback(() => resources.get(query), [resources, query])
  const snapshot = useSyncExternalStore(subscribe, get, get)
  useEffect(() => { if (active) void resources.load(query) }, [resources, query, active, foregroundSignal])
  return { ...snapshot, retry: () => resources.retry(query), refresh: () => resources.load(query, 'refresh'), more: () => resources.load(query, 'more'), fills: () => resources.load(query, 'fills'), events: () => resources.load(query, 'events') }
}
