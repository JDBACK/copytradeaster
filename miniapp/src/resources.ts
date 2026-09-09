/** Session-scoped server DTO owner for S2 (§19).
 *
 * This is intentionally small and fixed to the Mini App resources we share
 * across screen lifetimes. It is not a generic client cache: no persistence,
 * no localStorage, no secrets, no wallet/signature/confirmation state.
 */

import { HistoryResources } from './historyResources'
import { api, ApiError, type AuthContext } from './api'
import type {
  AccountBuilderDTO,
  DashboardDTO,
  NotificationSettingsDTO,
  PositionPageDTO,
  RiskDTO,
  RiskPreviewDTO,
  StatusDTO,
} from './types'

export type ResourceKey =
  | 'dashboard'
  | 'positions'
  | 'risk'
  | 'riskPreview'
  | 'notifications'
  | 'status'
  | 'accountBuilder'

export interface ResourceValues {
  dashboard: DashboardDTO
  positions: PositionPageDTO
  risk: RiskDTO
  riskPreview: RiskPreviewDTO
  notifications: NotificationSettingsDTO
  status: StatusDTO
  accountBuilder: AccountBuilderDTO
}

export type ReadStrength = 'weak' | 'strong'

export const RESOURCE_TTL_MS: Record<ResourceKey, number> = {
  dashboard: 30_000,
  positions: 30_000,
  risk: 60_000,
  riskPreview: 30_000,
  notifications: 30_000,
  status: 30_000,
  accountBuilder: 30_000,
}

export type MutationKey = 'pause-resume' | 'risk' | 'notifications' | 'account-builder'
export type MutationUnknownReason =
  | 'pause'
  | 'resume'
  | 'risk'
  | 'notifications'
  | 'account-reauthorize'
  | 'account-disconnect'

type MutationLock =
  | { state: 'active'; token: symbol }
  | { state: 'unknown'; reason: MutationUnknownReason; sinceRevision: number }

export interface ResourceSnapshot<T> {
  data: T | null
  receivedAt: number
  stale: boolean
  inflight: boolean
  error: string | null
  revision: number
  /** Strength of the latest accepted read; confirmed mutation publications are strong. */
  strength: ReadStrength | null
}

interface Flight<T> {
  token: number
  invalidationAtStart: number
  strength: ReadStrength
  controller: AbortController
  promise: Promise<T>
}

interface Entry<T> {
  data: T | null
  receivedAt: number
  invalidationRevision: number
  dataInvalidationRevision: number
  generation: number
  barrierGeneration: number
  publishedGeneration: number
  weak: Flight<T> | null
  strong: Flight<T> | null
  error: string | null
  revision: number
  strength: ReadStrength | null
  snapshot: ResourceSnapshot<T>
  listeners: Set<() => void>
}

function emptyEntry<T>(): Entry<T> {
  const snapshot: ResourceSnapshot<T> = {
    data: null,
    receivedAt: 0,
    stale: true,
    inflight: false,
    error: null,
    revision: 0,
    strength: null,
  }
  return {
    data: null,
    receivedAt: 0,
    invalidationRevision: 0,
    dataInvalidationRevision: -1,
    generation: 0,
    barrierGeneration: 0,
    publishedGeneration: 0,
    weak: null,
    strong: null,
    error: null,
    revision: 0,
    strength: null,
    snapshot,
    listeners: new Set(),
  }
}

export class MiniAppResources {
  readonly scope: string
  readonly history: HistoryResources
  private readonly auth: AuthContext
  private readonly onInvalidSession?: () => void
  private invalidSessionReported = false
  private disposed = false
  private readonly locks = new Map<MutationKey, MutationLock>()
  private readonly entries: { [K in ResourceKey]: Entry<ResourceValues[K]> } = {
    dashboard: emptyEntry<DashboardDTO>(),
    positions: emptyEntry<PositionPageDTO>(),
    risk: emptyEntry<RiskDTO>(),
    riskPreview: emptyEntry<RiskPreviewDTO>(),
    notifications: emptyEntry<NotificationSettingsDTO>(),
    status: emptyEntry<StatusDTO>(),
    accountBuilder: emptyEntry<AccountBuilderDTO>(),
  }

  constructor(auth: AuthContext, scope: string, onInvalidSession?: () => void) {
    this.history = new HistoryResources(auth, this)
    this.auth = auth
    this.scope = scope
    this.onInvalidSession = onInvalidSession
  }

  getSnapshot<K extends ResourceKey>(key: K): ResourceSnapshot<ResourceValues[K]> {
    return this.entry(key).snapshot
  }

  peek<K extends ResourceKey>(key: K): ResourceValues[K] | null {
    return this.entry(key).data
  }

  hasSubscribers(key: ResourceKey): boolean {
    return this.entry(key).listeners.size > 0
  }

  subscribe<K extends ResourceKey>(key: K, listener: () => void): () => void {
    const entry = this.entry(key)
    entry.listeners.add(listener)
    return () => {
      entry.listeners.delete(listener)
      if (entry.listeners.size > 0) return
      // React StrictMode performs a synthetic unsubscribe/resubscribe. Defer
      // last-consumer release one microtask so the replacement subscription can
      // retain the shared flight. Mutation reconciliation is owner work rather
      // than screen work and intentionally survives Back/remount.
      queueMicrotask(() => {
        if (this.disposed || entry.listeners.size > 0 || this.resourceMutationProtected(key)) return
        this.cancelUnobservedRead(key, 'weak')
        this.cancelUnobservedRead(key, 'strong')
      })
    }
  }

  /** Cached read with typed weak/strong single-flight semantics. */
  ensure<K extends ResourceKey>(
    key: K,
    options: { strength?: ReadStrength; maxAgeMs?: number; force?: boolean } = {},
  ): Promise<ResourceValues[K]> {
    if (this.disposed) return Promise.reject(new ApiError(0, 'aborted', 'Resource scope disposed.'))
    const entry = this.entry(key)
    const strength = options.strength ?? 'weak'
    const maxAgeMs = options.maxAgeMs ?? RESOURCE_TTL_MS[key]
    const fresh =
      entry.data !== null &&
      entry.dataInvalidationRevision === entry.invalidationRevision &&
      Date.now() - entry.receivedAt < maxAgeMs &&
      (strength === 'weak' || entry.strength === 'strong')
    if (!options.force && fresh) return Promise.resolve(entry.data as ResourceValues[K])

    // A strong flight satisfies both readers; a strong request never joins a
    // weak flight because refresh=true may perform signed/network verification.
    if (entry.strong) return entry.strong.promise
    if (strength === 'weak' && entry.weak) return entry.weak.promise
    if (entry.data !== null && entry.dataInvalidationRevision === entry.invalidationRevision) {
      // Once a cached DTO is known to require a read (TTL, stronger evidence,
      // or explicit refresh), it must stop presenting as fresh while that read
      // is pending. Keep the data visible, but advance the invalidation fence
      // synchronously before the request starts.
      entry.invalidationRevision += 1
      entry.barrierGeneration = ++entry.generation
      this.publishSnapshot(entry)
    }
    return this.startFlight(key, strength)
  }

  refresh<K extends ResourceKey>(key: K, strength: ReadStrength = 'weak'): Promise<ResourceValues[K]> {
    return this.ensure(key, { strength, force: true, maxAgeMs: 0 })
  }

  /** Cancel speculative owner work only while nobody is subscribed to it.
   * Real screen consumers always win over prefetch cancellation. */
  cancelUnobservedRead(key: ResourceKey, strength: ReadStrength = 'weak'): boolean {
    if (this.disposed) return false
    const entry = this.entry(key)
    if (entry.listeners.size > 0) return false
    const flight = strength === 'strong' ? entry.strong : entry.weak
    if (!flight) return false
    if (strength === 'strong') entry.strong = null
    else entry.weak = null
    // Cancellation is also a publication fence: a test double or unusual
    // WebView body implementation may ignore AbortSignal and resolve late.
    entry.barrierGeneration = ++entry.generation
    flight.controller.abort('speculative_cancelled')
    this.publishSnapshot(entry)
    return true
  }

  /** Await the current invalidation revision, performing at most one follow-up
   * when the first await joined a request that predates an invalidation. */
  async refreshCurrent<K extends ResourceKey>(
    key: K,
    strength: ReadStrength = 'weak',
  ): Promise<ResourceValues[K]> {
    let data: ResourceValues[K]
    try {
      data = await this.refresh(key, strength)
    } catch (error) {
      const snapshot = this.getSnapshot(key)
      // A request admitted before a newer invalidation/mutation may reject
      // after it has already been fenced. finishFlight deliberately does not
      // publish that obsolete error, so stale=true + error=null means the
      // caller still owes exactly one post-boundary read. A current failure
      // publishes snapshot.error and must not be retried automatically.
      if (snapshot.stale && snapshot.error === null) {
        return this.refresh(key, strength)
      }
      throw error
    }
    if (this.getSnapshot(key).stale) data = await this.refresh(key, strength)
    return data
  }

  /** Mark dependent DTOs suspect and fence every read that started before it. */
  invalidate(keys: readonly ResourceKey[]): void {
    if (this.disposed) return
    for (const key of keys) {
      const entry = this.entry(key)
      entry.invalidationRevision += 1
      entry.barrierGeneration = ++entry.generation
      this.publishSnapshot(entry)
    }
  }

  /** Publish a server-confirmed mutation response and reject pre-mutation reads. */
  publishConfirmed<K extends ResourceKey>(key: K, data: ResourceValues[K]): void {
    if (this.disposed) return
    const entry = this.entry(key)
    entry.invalidationRevision += 1
    const generation = ++entry.generation
    entry.barrierGeneration = generation
    entry.publishedGeneration = generation
    entry.dataInvalidationRevision = entry.invalidationRevision
    entry.data = data
    entry.receivedAt = Date.now()
    entry.error = null
    entry.strength = 'strong'
    this.publishSnapshot(entry)
  }

  /** Session-lifetime mutation lock. The release survives nested unmount/Back. */
  acquireMutation(key: MutationKey): (() => void) | null {
    if (this.disposed || this.locks.has(key)) return null
    const token = Symbol(key)
    this.locks.set(key, { state: 'active', token })
    this.notifyMutationLock(key)
    let released = false
    return () => {
      if (released) return
      released = true
      const current = this.locks.get(key)
      if (current?.state !== 'active' || current.token !== token) return
      this.locks.delete(key)
      if (this.disposed) return
      this.notifyMutationLock(key)
    }
  }

  mutationLocked(key: MutationKey): boolean {
    return this.locks.has(key)
  }

  mutationState(key: MutationKey): MutationLock['state'] | null {
    return this.locks.get(key)?.state ?? null
  }

  mutationUnknownReason(key: MutationKey): MutationUnknownReason | null {
    const current = this.locks.get(key)
    return current?.state === 'unknown' ? current.reason : null
  }

  mutationUnknownSinceRevision(key: MutationKey): number | null {
    const current = this.locks.get(key)
    return current?.state === 'unknown' ? current.sinceRevision : null
  }

  /** Retain a mutation whose transport outcome and reconciliation are both unknown. */
  markMutationUnknown(key: MutationKey, reason: MutationUnknownReason): boolean {
    if (this.disposed) return false
    const current = this.locks.get(key)
    if (current?.state !== 'active') return false
    const resourceKey = this.mutationResourceKey(key)
    // notifyMutationLock below advances this snapshot once. Record that exact
    // boundary so only evidence published afterwards can resolve the unknown.
    const sinceRevision = this.entry(resourceKey).snapshot.revision + 1
    this.locks.set(key, { state: 'unknown', reason, sinceRevision })
    this.notifyMutationLock(key)
    return true
  }

  /** Authoritative reconciliation may clear retained uncertainty, never an active flight. */
  resolveUnknownMutation(key: MutationKey): boolean {
    if (this.disposed) return false
    const current = this.locks.get(key)
    if (current?.state !== 'unknown') return false
    this.locks.delete(key)
    this.notifyMutationLock(key)
    return true
  }

  /**
   * Route a transport error through the same hard session boundary used by
   * resource-owned reads. Direct screen reads (Activity in S2) call this so a
   * 401/invalid_session cannot remain a local screen error. The notification is
   * idempotent for the lifetime of this owner.
   */
  handleInvalidSessionError(error: unknown): boolean {
    if (
      !(error instanceof ApiError) ||
      (error.status !== 401 && error.code !== 'invalid_session')
    ) {
      return false
    }
    // A late direct read from a scope already disposed for another boundary
    // must not resurrect routing. It is nevertheless a handled auth error.
    if (this.disposed) return true
    const notify = !this.invalidSessionReported
    this.invalidSessionReported = true
    this.dispose()
    if (notify) this.onInvalidSession?.()
    return true
  }

  /** Hard scope boundary: abort all reads and make late publications impossible. */
  dispose(): void {
    if (this.disposed) return
    this.disposed = true
    this.history.dispose()
    this.locks.clear()
    for (const key of Object.keys(this.entries) as ResourceKey[]) {
      const entry = this.entry(key)
      entry.weak?.controller.abort('scope_disposed')
      entry.strong?.controller.abort('scope_disposed')
      entry.weak = null
      entry.strong = null
      entry.data = null
      entry.error = null
      entry.receivedAt = 0
      entry.strength = null
      entry.invalidationRevision += 1
      entry.barrierGeneration = ++entry.generation
      this.publishSnapshot(entry)
    }
  }

  private startFlight<K extends ResourceKey>(key: K, strength: ReadStrength): Promise<ResourceValues[K]> {
    const entry = this.entry(key)
    const controller = new AbortController()
    const token = ++entry.generation
    const invalidationAtStart = entry.invalidationRevision
    const promise = this.read(key, strength, controller.signal)
    const flight: Flight<ResourceValues[K]> = {
      token,
      invalidationAtStart,
      strength,
      controller,
      promise,
    }
    if (strength === 'strong') entry.strong = flight
    else entry.weak = flight
    entry.error = null
    this.publishSnapshot(entry)

    void promise.then(
      (data) => this.finishFlight(key, flight, data, null),
      (error: unknown) => this.finishFlight(key, flight, null, error),
    )
    return promise
  }

  private finishFlight<K extends ResourceKey>(
    key: K,
    flight: Flight<ResourceValues[K]>,
    data: ResourceValues[K] | null,
    error: unknown,
  ): void {
    const entry = this.entry(key)
    if (entry.weak === flight) entry.weak = null
    if (entry.strong === flight) entry.strong = null
    if (this.disposed) return

    const fenced = flight.token < entry.barrierGeneration
    const superseded = flight.token < entry.publishedGeneration
    const invalidatedDuringFlight = flight.invalidationAtStart !== entry.invalidationRevision

    if (error !== null) {
      // Authentication loss is a hard scope boundary, not a per-screen
      // retryable error. Abort every sibling flight and fence all late DTOs
      // before routing the application back to the session gate.
      if (this.handleInvalidSessionError(error)) return
      // A losing/obsolete request cannot poison the error slot after a newer
      // strong read or confirmed mutation has already published.
      if (!fenced && !superseded && !invalidatedDuringFlight && !(error instanceof ApiError && error.code === 'aborted')) {
        entry.error = error instanceof ApiError ? error.code : 'network_error'
      }
      this.publishSnapshot(entry)
      return
    }

    if (!fenced && !superseded && !invalidatedDuringFlight && data !== null) {
      entry.data = data
      entry.receivedAt = Date.now()
      entry.dataInvalidationRevision = entry.invalidationRevision
      entry.publishedGeneration = flight.token
      entry.error = null
      entry.strength = flight.strength
    }
    this.publishSnapshot(entry)

    // A real dashboard refresh defines a newer server snapshot. Any positions
    // read that predated it must be fenced, and active consumers will perform
    // exactly one follow-up through useResource.
    if (key === 'dashboard' && flight.strength === 'strong' && !fenced && !superseded && !invalidatedDuringFlight) {
      this.invalidate(['positions'])
    }
  }

  private read<K extends ResourceKey>(
    key: K,
    strength: ReadStrength,
    signal: AbortSignal,
  ): Promise<ResourceValues[K]> {
    switch (key) {
      case 'dashboard':
        return api.dashboard(this.auth, strength === 'strong', { signal }) as Promise<ResourceValues[K]>
      case 'positions':
        return api.positions(this.auth, strength === 'strong', { signal }) as Promise<ResourceValues[K]>
      case 'riskPreview':
        return api.riskPreview(this.auth, { signal }) as Promise<ResourceValues[K]>
      case 'risk':
        return api.risk(this.auth, { signal }) as Promise<ResourceValues[K]>
      case 'notifications':
        return api.notifications(this.auth, { signal }) as Promise<ResourceValues[K]>
      case 'status':
        return api.status(this.auth, strength === 'strong', { signal }) as Promise<ResourceValues[K]>
      case 'accountBuilder':
        return api.accountBuilder(this.auth, strength === 'strong', { signal }) as Promise<ResourceValues[K]>
    }
  }

  private entry<K extends ResourceKey>(key: K): Entry<ResourceValues[K]> {
    return this.entries[key] as Entry<ResourceValues[K]>
  }

  private publishSnapshot<T>(entry: Entry<T>): void {
    entry.revision += 1
    entry.snapshot = {
      data: entry.data,
      receivedAt: entry.receivedAt,
      stale: entry.data === null || entry.dataInvalidationRevision !== entry.invalidationRevision,
      inflight: entry.weak !== null || entry.strong !== null,
      error: entry.error,
      revision: entry.revision,
      strength: entry.strength,
    }
    for (const listener of entry.listeners) listener()
  }

  private notifyMutationLock(key: MutationKey): void {
    const resourceKey = this.mutationResourceKey(key)
    this.publishSnapshot(this.entry(resourceKey))
  }

  private mutationResourceKey(key: MutationKey): ResourceKey {
    return key === 'pause-resume'
      ? 'dashboard'
      : key === 'account-builder'
        ? 'accountBuilder'
        : key
  }

  private resourceMutationProtected(key: ResourceKey): boolean {
    for (const mutationKey of this.locks.keys()) {
      if (this.mutationResourceKey(mutationKey) === key) return true
    }
    return false
  }
}
