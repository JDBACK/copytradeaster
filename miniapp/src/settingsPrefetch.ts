import {
  MiniAppResources,
  RESOURCE_TTL_MS,
  type ResourceKey,
} from './resources'

/** Cheap nested Settings reads, in the order they should be speculated. */
export const SETTINGS_PREFETCH_KEYS = [
  'risk',
  'notifications',
  'status',
  'accountBuilder',
] as const satisfies readonly ResourceKey[]

export type SettingsPrefetchKey = (typeof SETTINGS_PREFETCH_KEYS)[number]

const MAX_SPECULATIVE_READS = 2

/**
 * Small scheduler for nested Settings warmup.
 *
 * It owns speculative queue slots and may cancel only an unobserved weak owner
 * flight. Reads always go through the session-scoped resource owner, which
 * refuses cancellation once a real screen consumer is subscribed. This module
 * intentionally has no path that can request a strong read.
 */
export class SettingsPrefetch {
  private active = false
  private queue: SettingsPrefetchKey[] = []
  private readonly running = new Map<SettingsPrefetchKey, Promise<unknown>>()
  private userTarget: SettingsPrefetchKey | null = null
  private readonly resources: MiniAppResources

  constructor(resources: MiniAppResources) {
    this.resources = resources
  }

  /** Start one ordered warmup pass. Repeated calls while active are a no-op. */
  start(): void {
    if (this.active) return
    this.active = true
    this.queue = SETTINGS_PREFETCH_KEYS.filter((key) => !this.running.has(key))
    this.pump()
  }

  /**
   * Stop speculative scheduling. Unobserved already-started weak prefetches are
   * released; a flight already joined by a mounted screen is left alive.
   */
  stop(): void {
    this.active = false
    this.queue = []
    for (const key of [...this.running.keys()]) {
      // A row click has already promoted this read from speculation to a
      // user-requested target. The nested screen about to mount must join it,
      // not observe an abort and start a duplicate request.
      if (key === this.userTarget) {
        if (this.resources.hasSubscribers(key)) {
          this.userTarget = null
        } else {
          // React may run the Settings effect cleanup and its inactive setup
          // before the nested useSyncExternalStore subscription commits. Keep
          // the target protected across both stops, then demote as soon as the
          // real nested consumer is observable.
          queueMicrotask(() => {
            if (this.userTarget === key && this.resources.hasSubscribers(key)) {
              this.userTarget = null
            }
          })
        }
        continue
      }
      if (this.resources.cancelUnobservedRead(key, 'weak')) this.running.delete(key)
    }
  }

  /**
   * Give a clicked nested row priority. A running or fresh target is already
   * satisfied and is not duplicated; otherwise the queued target moves to the
   * front and starts immediately when a speculative slot is available.
   */
  prioritize(key: SettingsPrefetchKey): void {
    if (this.isFresh(key)) return
    this.userTarget = key
    const targetAlreadyRunning = this.running.has(key)

    const index = this.queue.indexOf(key)
    if (index >= 0) this.queue.splice(index, 1)
    this.queue.unshift(key)
    if (!this.active) return

    // A user-selected target outranks speculative tail work. If both slots are
    // occupied by unobserved prefetch, release one before the nested screen
    // mounts so its target read never becomes a third concurrent request.
    if (this.running.size >= MAX_SPECULATIVE_READS) {
      const candidates = [...this.running.keys()].reverse()
      for (const runningKey of candidates) {
        if (runningKey === key) continue
        if (this.resources.cancelUnobservedRead(runningKey, 'weak')) {
          this.running.delete(runningKey)
          break
        }
      }
    }
    if (targetAlreadyRunning) return
    this.pump()
  }

  private pump(): void {
    while (this.active && this.running.size < MAX_SPECULATIVE_READS && this.queue.length > 0) {
      const key = this.queue.shift()
      if (!key || this.running.has(key) || this.isFresh(key)) continue

      // Prefetch is always weak. MiniAppResources decides whether this joins an
      // equivalent/strong owner flight or starts a new weak request.
      const promise = this.resources.ensure(key, { strength: 'weak' })
      this.running.set(key, promise)
      void promise.then(
        () => this.settle(key, promise),
        () => this.settle(key, promise),
      )
    }
  }

  private settle(key: SettingsPrefetchKey, promise: Promise<unknown>): void {
    if (this.running.get(key) !== promise) return
    this.running.delete(key)
    if (this.userTarget === key) this.userTarget = null
    const snapshot = this.resources.getSnapshot(key)
    // Last-consumer/preemption cancellation is fenced and intentionally does
    // not publish an error. If Settings is visible again, the current warmup
    // pass still owes that absent/stale DTO. Requeue it in the declared fixed
    // order. A real failed read publishes snapshot.error and is deliberately
    // not retried here, preserving the no-auto-loop contract.
    if (this.active && !this.isFresh(key) && snapshot.error === null && !this.queue.includes(key)) {
      const order = SETTINGS_PREFETCH_KEYS.indexOf(key)
      const insertion = this.queue.findIndex((queued) => SETTINGS_PREFETCH_KEYS.indexOf(queued) > order)
      if (insertion === -1) this.queue.push(key)
      else this.queue.splice(insertion, 0, key)
    }
    if (this.active) this.pump()
  }

  private isFresh(key: SettingsPrefetchKey): boolean {
    const snapshot = this.resources.getSnapshot(key)
    return (
      snapshot.data !== null &&
      !snapshot.stale &&
      Date.now() - snapshot.receivedAt < RESOURCE_TTL_MS[key]
    )
  }
}

export function createSettingsPrefetch(resources: MiniAppResources): SettingsPrefetch {
  return new SettingsPrefetch(resources)
}
