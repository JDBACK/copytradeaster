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
  // A healthy transport is not news. This rail exists for the one case a user
  // has to act on: the stream is down long enough that automatic recovery has
  // already failed, so the numbers on screen stopped moving. "LIVE",
  // "connecting" and every transient blip stay out of the way.
  if (health !== 'reconnecting' && health !== 'stale') return null
  return (
    <div className={`live-status live-status-${health}`} role="status" aria-live="polite">
      <span className="live-status-dot" aria-hidden />
      <span>{liveStatusLabel(snapshot, now || snapshot.lastUpdatedAt)}</span>
      {/* No retry button: reconnects keep retrying by themselves, and the whole
          surface refreshes on a pull-down, so the user is never asked to press
          anything to make data current. */}
    </div>
  )
}
