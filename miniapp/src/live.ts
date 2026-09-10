import { api, ApiError, type AuthContext } from './api'

export type LiveHealth = 'connecting' | 'live' | 'reconnecting' | 'stale'

export interface LiveSnapshot {
  health: LiveHealth
  revision: number
  lastUpdatedAt: number
  retrying: boolean
}

export interface LiveUpdate {
  resources: readonly string[]
  revision: number
}

const STALE_AFTER_MS = 12_000
const RECONNECT_MAX_MS = 15_000
const HEARTBEAT_TIMEOUT_MS = 30_000
const FULL_RECONCILE_RESOURCES = [
  'dashboard',
  'positions',
  'risk',
  'riskPreview',
  'notifications',
  'status',
  'accountBuilder',
  'history',
] as const

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

export class MiniAppLiveSession {
  private readonly auth: AuthContext
  private readonly onUpdate: (event: LiveUpdate) => void
  private readonly onInvalidSession: (error: ApiError) => void
  private readonly listeners = new Set<() => void>()
  private socket: WebSocket | null = null
  private ticketAbort: AbortController | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private staleTimer: ReturnType<typeof setTimeout> | null = null
  private heartbeatTimer: ReturnType<typeof setTimeout> | null = null
  private lifecycleAttached = false
  private disposed = false
  private generation = 0
  private reconnectAttempt = 0
  private established = false
  private snapshot: LiveSnapshot = {
    health: 'connecting',
    revision: 0,
    lastUpdatedAt: 0,
    retrying: false,
  }

  constructor(
    auth: AuthContext,
    onUpdate: (event: LiveUpdate) => void,
    onInvalidSession: (error: ApiError) => void,
  ) {
    this.auth = auth
    this.onUpdate = onUpdate
    this.onInvalidSession = onInvalidSession
  }

  getSnapshot = (): LiveSnapshot => this.snapshot

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  start(): void {
    if (this.disposed || this.socket || this.ticketAbort) return
    this.attachLifecycle()
    void this.connect()
  }

  retryNow(): void {
    if (this.disposed) return
    this.clearReconnectTimer()
    this.clearStaleTimer()
    this.clearHeartbeatTimer()
    // Fence callbacks from the connection being replaced. Otherwise an old
    // onclose can schedule a second reconnect and later create a duplicate WS.
    this.generation += 1
    this.ticketAbort?.abort()
    this.ticketAbort = null
    this.socket?.close()
    this.socket = null
    this.reconnectAttempt = 0
    this.publish({ health: 'connecting', retrying: true })
    void this.connect()
  }

  dispose(): void {
    if (this.disposed) return
    this.disposed = true
    this.generation += 1
    this.clearReconnectTimer()
    this.clearStaleTimer()
    this.clearHeartbeatTimer()
    this.detachLifecycle()
    this.ticketAbort?.abort()
    this.ticketAbort = null
    this.socket?.close()
    this.socket = null
    this.listeners.clear()
  }

  private async connect(): Promise<void> {
    if (this.disposed || typeof WebSocket === 'undefined') return
    const generation = ++this.generation
    const controller = new AbortController()
    this.ticketAbort = controller
    this.publish({
      health: this.snapshot.lastUpdatedAt ? 'reconnecting' : 'connecting',
      retrying: this.reconnectAttempt > 0,
    })
    this.armStaleTimer()
    try {
      const grant = await api.liveTicket(this.auth, { timeoutMs: 8_000, signal: controller.signal })
      if (this.disposed || generation !== this.generation) return
      const socket = new WebSocket(this.socketUrl(grant.ticket))
      this.socket = socket
      this.armHeartbeatTimer(generation)
      socket.onmessage = (message) => this.onMessage(generation, message.data)
      socket.onerror = () => socket.close()
      socket.onclose = () => this.onClose(generation)
    } catch (error) {
      if (this.disposed || generation !== this.generation) return
      if (error instanceof ApiError && (error.status === 401 || error.code === 'invalid_session')) {
        this.onInvalidSession(error)
        return
      }
      this.scheduleReconnect()
    } finally {
      if (generation === this.generation) this.ticketAbort = null
    }
  }

  private onMessage(generation: number, raw: unknown): void {
    if (this.disposed || generation !== this.generation || typeof raw !== 'string') return
    let value: unknown
    try {
      value = JSON.parse(raw)
    } catch {
      return
    }
    if (!isRecord(value) || typeof value.revision !== 'number' || !Number.isFinite(value.revision)) return
    this.armHeartbeatTimer(generation)
    const revision = value.revision
    if (value.type === 'heartbeat') return
    if (revision < this.snapshot.revision || (revision === this.snapshot.revision && value.type !== 'hello')) return
    const previousRevision = this.snapshot.revision
    const revisionGap = revision > previousRevision + 1
    const reconnectGap = value.type === 'hello' && this.established && revision > previousRevision
    const health = value.health
    const nextHealth: LiveHealth | null =
      health === 'live' || health === 'connecting' || health === 'reconnecting' ? health : null
    const updatedAt = typeof value.updated_at === 'string' ? Date.parse(value.updated_at) : Number.NaN
    this.reconnectAttempt = 0
    this.clearReconnectTimer()
    if (nextHealth === 'live') this.clearStaleTimer()
    else if (nextHealth) this.armStaleTimer()
    this.publish({
      revision,
      ...(Number.isFinite(updatedAt) ? { lastUpdatedAt: updatedAt } : {}),
      retrying: false,
      ...(nextHealth ? { health: nextHealth } : {}),
    })
    if (value.type === 'hello') this.established = true
    if (revisionGap || reconnectGap) {
      this.onUpdate({ resources: FULL_RECONCILE_RESOURCES, revision })
    } else if (
      value.type === 'update' &&
      Array.isArray(value.resources) &&
      value.resources.every((item) => typeof item === 'string')
    ) {
      this.onUpdate({ resources: value.resources, revision })
    }
  }

  private onClose(generation: number): void {
    if (this.disposed || generation !== this.generation) return
    this.socket = null
    this.clearHeartbeatTimer()
    this.publish({ health: 'reconnecting', retrying: true })
    this.armStaleTimer()
    this.scheduleReconnect()
  }

  private scheduleReconnect(): void {
    if (this.disposed || this.reconnectTimer) return
    this.publish({ health: 'reconnecting', retrying: true })
    this.armStaleTimer()
    const delay = Math.min(1_000 * 2 ** this.reconnectAttempt, RECONNECT_MAX_MS)
    this.reconnectAttempt += 1
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      void this.connect()
    }, delay)
  }

  private armStaleTimer(): void {
    if (this.staleTimer || this.disposed) return
    this.staleTimer = setTimeout(() => {
      this.staleTimer = null
      if (!this.disposed && this.snapshot.health !== 'live') {
        this.publish({ health: 'stale', retrying: true })
      }
    }, STALE_AFTER_MS)
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer)
    this.reconnectTimer = null
  }

  private clearStaleTimer(): void {
    if (this.staleTimer) clearTimeout(this.staleTimer)
    this.staleTimer = null
  }

  private armHeartbeatTimer(generation: number): void {
    this.clearHeartbeatTimer()
    if (
      this.disposed ||
      (typeof document !== 'undefined' && document.visibilityState === 'hidden')
    ) return
    this.heartbeatTimer = setTimeout(() => {
      this.heartbeatTimer = null
      if (this.disposed || generation !== this.generation) return
      this.publish({ health: 'stale', retrying: true })
      this.socket?.close()
    }, HEARTBEAT_TIMEOUT_MS)
  }

  private clearHeartbeatTimer(): void {
    if (this.heartbeatTimer) clearTimeout(this.heartbeatTimer)
    this.heartbeatTimer = null
  }

  private readonly handleForeground = (): void => {
    if (
      this.disposed ||
      typeof document === 'undefined' ||
      document.visibilityState !== 'visible'
    ) return
    if (this.socket) {
      this.armHeartbeatTimer(this.generation)
      return
    }
    if (!this.ticketAbort) this.retryNow()
  }

  private readonly handleVisibility = (): void => {
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
      this.clearHeartbeatTimer()
      return
    }
    this.handleForeground()
  }

  private attachLifecycle(): void {
    if (
      this.lifecycleAttached ||
      typeof window === 'undefined' ||
      typeof document === 'undefined'
    ) return
    this.lifecycleAttached = true
    window.addEventListener('focus', this.handleForeground)
    document.addEventListener('visibilitychange', this.handleVisibility)
  }

  private detachLifecycle(): void {
    if (
      !this.lifecycleAttached ||
      typeof window === 'undefined' ||
      typeof document === 'undefined'
    ) return
    this.lifecycleAttached = false
    window.removeEventListener('focus', this.handleForeground)
    document.removeEventListener('visibilitychange', this.handleVisibility)
  }

  private socketUrl(ticket: string): string {
    const url = new URL('/miniapp/api/live', window.location.href)
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
    url.searchParams.set('ticket', ticket)
    return url.toString()
  }

  private publish(patch: Partial<LiveSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch }
    for (const listener of this.listeners) listener()
  }
}
