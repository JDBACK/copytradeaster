import { useEffect, useState, useSyncExternalStore } from 'react'
import type { LiveHealth } from '../live'
import { liveStatusLabel } from '../liveStatus'
import type { MiniAppResources } from '../resources'

export function LiveStatus({ resources }: { resources: MiniAppResources }) {
  const snapshot = useSyncExternalStore(
    resources.subscribeLive,
    resources.getLiveSnapshot,
    resources.getLiveSnapshot,
  )
  const [now, setNow] = useState(0)

  useEffect(() => {
    if (snapshot.health !== 'stale') return
    const initial = setTimeout(() => setNow(Date.now()), 0)
    const timer = setInterval(() => setNow(Date.now()), 1_000)
    return () => {
      clearTimeout(initial)
      clearInterval(timer)
    }
  }, [snapshot.health])

  const health: LiveHealth = snapshot.health
  return (
    <div className={`live-status live-status-${health}`} role="status" aria-live="polite">
      <span className="live-status-dot" aria-hidden />
      <span>{liveStatusLabel(snapshot, now || snapshot.lastUpdatedAt)}</span>
      {health === 'stale' && (
        <button type="button" className="live-status-retry" onClick={() => resources.retryLive()}>
          Повторить
        </button>
      )}
    </div>
  )
}
