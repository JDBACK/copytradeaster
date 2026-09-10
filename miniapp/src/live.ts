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
  private disposed = false
  private generation = 0
  private reconnectAttempt = 0
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
    void this.connect()
  }

  retryNow(): void {
    if (this.disposed) return
    this.clearReconnectTimer()
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
    const revision = value.revision
    if (revision < this.snapshot.revision) return
    const health = value.health
    const nextHealth: LiveHealth | null =
      health === 'live' || health === 'connecting' || health === 'reconnecting' ? health : null
    const updatedAt = typeof value.updated_at === 'string' ? Date.parse(value.updated_at) : Date.now()
    this.reconnectAttempt = 0
    this.clearReconnectTimer()
    if (nextHealth === 'live') this.clearStaleTimer()
    else if (nextHealth) this.armStaleTimer()
    this.publish({
      revision,
      lastUpdatedAt: Number.isFinite(updatedAt) ? updatedAt : Date.now(),
      retrying: false,
      ...(nextHealth ? { health: nextHealth } : {}),
    })
    if (
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
