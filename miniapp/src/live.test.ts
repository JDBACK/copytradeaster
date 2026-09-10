// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from './api'
import { MiniAppLiveSession } from './live'

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  readonly url: string
  closed = false
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }

  close(): void {
    if (this.closed) return
    this.closed = true
    this.onclose?.()
  }

  message(payload: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent)
  }
}

const flush = async () => {
  await Promise.resolve()
  await Promise.resolve()
}

describe('MiniAppLiveSession', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket)
    vi.spyOn(api, 'liveTicket').mockResolvedValue({ ticket: 'ticket-1', expires_in: 20 })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('accepts hello and one newer update while ignoring duplicate/out-of-order revisions', async () => {
    const updates = vi.fn()
    const session = new MiniAppLiveSession({ initData: 'signed' }, updates, vi.fn())
    session.start()
    await flush()

    const socket = FakeWebSocket.instances[0]
    socket.message({ type: 'hello', revision: 0, health: 'live', updated_at: '2026-09-10T03:00:00Z' })
    socket.message({ type: 'update', revision: 1, resources: ['positions'] })
    socket.message({ type: 'update', revision: 1, resources: ['dashboard'] })
    socket.message({ type: 'update', revision: 0, resources: ['history'] })

    expect(session.getSnapshot().health).toBe('live')
    expect(session.getSnapshot().revision).toBe(1)
    expect(updates).toHaveBeenCalledTimes(1)
    expect(updates).toHaveBeenCalledWith({ resources: ['positions'], revision: 1 })
    session.dispose()
  })

  it('preserves snapshot source and capture boundary on an ordinary update', async () => {
    const updates = vi.fn()
    const session = new MiniAppLiveSession({ initData: 'signed' }, updates, vi.fn())
    session.start()
    await flush()
    const socket = FakeWebSocket.instances[0]
    socket.message({ type: 'hello', revision: 0, health: 'live', updated_at: '2026-09-10T03:00:00Z' })
    socket.message({
      type: 'update',
      revision: 1,
      resources: ['dashboard', 'positions'],
      source: 'rest',
      captured_at: '2026-09-10T03:00:01.123456+00:00',
    })

    expect(updates).toHaveBeenCalledWith({
      resources: ['dashboard', 'positions'],
      revision: 1,
      source: 'rest',
      capturedAt: '2026-09-10T03:00:01.123456+00:00',
    })
    session.dispose()
  })

  it('turns a revision gap into one full live-resource reconciliation', async () => {
    const updates = vi.fn()
    const session = new MiniAppLiveSession({ initData: 'signed' }, updates, vi.fn())
    session.start()
    await flush()
    const socket = FakeWebSocket.instances[0]
    socket.message({ type: 'hello', revision: 0, health: 'live', updated_at: '2026-09-10T03:00:00Z' })
    socket.message({ type: 'update', revision: 2, resources: ['history'] })
    expect(updates).toHaveBeenCalledTimes(1)
    const resources = updates.mock.calls[0]?.[0]?.resources as readonly string[]
    expect(resources).toContain('dashboard')
    expect(resources).toContain('positions')
    expect(resources).toContain('history')
    session.dispose()
  })

  it('reconciles missed revisions announced by a reconnect hello', async () => {
    const updates = vi.fn()
    const session = new MiniAppLiveSession({ initData: 'signed' }, updates, vi.fn())
    session.start()
    await flush()
    FakeWebSocket.instances[0].message({
      type: 'hello', revision: 1, health: 'live', updated_at: '2026-09-10T03:00:00Z',
    })
    session.retryNow()
    await flush()
    FakeWebSocket.instances[1].message({
      type: 'hello', revision: 4, health: 'live', updated_at: '2026-09-10T03:00:03Z',
    })
    expect(updates).toHaveBeenCalledTimes(1)
    expect(updates.mock.calls[0]?.[0]?.revision).toBe(4)
    session.dispose()
  })

  it('does not move last-good age forward on health-only frames', async () => {
    const session = new MiniAppLiveSession({ initData: 'signed' }, vi.fn(), vi.fn())
    session.start()
    await flush()
    const socket = FakeWebSocket.instances[0]
    socket.message({ type: 'hello', revision: 0, health: 'live', updated_at: '2026-09-10T03:00:00Z' })
    const lastGood = session.getSnapshot().lastUpdatedAt
    socket.message({ type: 'health', revision: 1, health: 'reconnecting' })
    expect(session.getSnapshot().lastUpdatedAt).toBe(lastGood)
    session.dispose()
  })

  it('application heartbeat resets the half-open watchdog without publishing a data update', async () => {
    const updates = vi.fn()
    const session = new MiniAppLiveSession({ initData: 'signed' }, updates, vi.fn())
    session.start()
    await flush()
    const socket = FakeWebSocket.instances[0]
    socket.message({ type: 'hello', revision: 0, health: 'live', updated_at: '2026-09-10T03:00:00Z' })

    vi.advanceTimersByTime(29_000)
    socket.message({ type: 'heartbeat', revision: 0 })
    vi.advanceTimersByTime(29_000)
    expect(socket.closed).toBe(false)
    expect(updates).not.toHaveBeenCalled()
    session.dispose()
  })

  it('manual retry fences the old close callback and creates only one replacement socket', async () => {
    const session = new MiniAppLiveSession({ initData: 'signed' }, vi.fn(), vi.fn())
    session.start()
    await flush()
    expect(FakeWebSocket.instances).toHaveLength(1)

    session.retryNow()
    await flush()
    expect(FakeWebSocket.instances).toHaveLength(2)
    vi.advanceTimersByTime(2_000)
    await flush()
    expect(FakeWebSocket.instances).toHaveLength(2)
    session.dispose()
  })

  it('manual retry restarts the stale deadline instead of inheriting the old connection timer', async () => {
    const session = new MiniAppLiveSession({ initData: 'signed' }, vi.fn(), vi.fn())
    session.start()
    await flush()
    vi.advanceTimersByTime(10_000)
    session.retryNow()
    await flush()
    vi.advanceTimersByTime(3_000)
    expect(session.getSnapshot().health).not.toBe('stale')
    session.dispose()
  })

  it('re-baselines on a restarted backend epoch instead of latching stale on lower revisions', async () => {
    const updates = vi.fn()
    const session = new MiniAppLiveSession({ initData: 'signed' }, updates, vi.fn())
    session.start()
    await flush()
    const first = FakeWebSocket.instances[0]
    first.message({
      type: 'hello', epoch: 'run-1', revision: 20, health: 'live',
      updated_at: '2026-09-10T03:00:00Z',
    })
    first.message({ type: 'health', epoch: 'run-1', revision: 21, health: 'reconnecting' })
    await vi.advanceTimersByTimeAsync(12_100)
    expect(session.getSnapshot().health).toBe('stale')

    // The backend restarts: a new process epoch whose revision counter starts
    // over. Its (lower) revisions must not be discarded as out-of-order, or
    // the rail stays "stale" although the transport recovered.
    session.retryNow()
    await flush()
    const second = FakeWebSocket.instances[1]
    second.message({
      type: 'hello', epoch: 'run-2', revision: 1, health: 'reconnecting',
      updated_at: '2026-09-10T03:05:00Z',
    })
    second.message({ type: 'health', epoch: 'run-2', revision: 2, health: 'live' })

    expect(session.getSnapshot().health).toBe('live')
    expect(session.getSnapshot().revision).toBe(2)
    const resources = updates.mock.calls[0]?.[0]?.resources as readonly string[]
    expect(resources).toContain('dashboard')
    expect(resources).toContain('positions')
    session.dispose()
  })

  it('becomes stale after the deadline even while automatic reconnect attempts keep failing', async () => {
    const session = new MiniAppLiveSession({ initData: 'signed' }, vi.fn(), vi.fn())
    session.start()
    await flush()
    const socket = FakeWebSocket.instances[0]
    socket.message({ type: 'hello', revision: 1, health: 'live', updated_at: '2026-09-10T03:00:00Z' })
    vi.mocked(api.liveTicket).mockRejectedValue(new ApiError(0, 'network_error', 'offline'))

    socket.close()
    expect(session.getSnapshot().health).toBe('reconnecting')

    await vi.advanceTimersByTimeAsync(12_100)
    expect(session.getSnapshot().health).toBe('stale')
    // Backoff attempts continue in the background, but once the outage crossed
    // the stale boundary they must not cosmetically downgrade the rail back to
    // "reconnecting" until an authoritative hello actually recovers it.
    await vi.advanceTimersByTimeAsync(20_000)
    expect(session.getSnapshot().health).toBe('stale')
    session.dispose()
  })

  it('routes invalid_session during ticket acquisition through the hard session boundary', async () => {
    vi.mocked(api.liveTicket).mockRejectedValueOnce(new ApiError(401, 'invalid_session', 'expired'))
    const invalid = vi.fn()
    const session = new MiniAppLiveSession({ initData: 'signed' }, vi.fn(), invalid)
    session.start()
    await flush()
    expect(invalid).toHaveBeenCalledTimes(1)
    expect(FakeWebSocket.instances).toHaveLength(0)
    session.dispose()
  })
})
