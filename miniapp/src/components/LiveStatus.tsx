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
  // The rail exists for exactly one situation: the numbers on screen stopped
  // moving. "LIVE", "Подключаем…" and "Восстанавливаем связь…" all describe the
  // transport doing its job — reconnecting is normal background work and every
  // blip of it used to flash here for a second or two. Only the stale verdict
  // (the channel has been silent past its deadline) is worth the user's
  // attention, and then it carries the age and no button.
  if (health !== 'stale') return null
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
