import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from './api'
import { MiniAppResources } from './resources'
import type { PositionPageDTO } from './types'

const AUTH = { initData: 'signed-test' }

function positionPage(count: number, capturedAt = `2026-09-07T12:00:0${count}+00:00`): PositionPageDTO {
  return {
    positions: [],
    count,
    system_state: 'ready',
    snapshot_captured_at: capturedAt,
    is_stale: false,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

async function flush(): Promise<void> {
  await Promise.resolve()
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('MiniAppResources read generations', () => {
  it('does not let a strong request join an existing weak flight', async () => {
    const weakGate = deferred<PositionPageDTO>()
    const strongGate = deferred<PositionPageDTO>()
    const positions = vi
      .spyOn(api, 'positions')
      .mockReturnValueOnce(weakGate.promise)
      .mockReturnValueOnce(strongGate.promise)
    const resources = new MiniAppResources(AUTH, 'scope-a')

    const weak = resources.ensure('positions', { strength: 'weak', force: true })
    const strong = resources.ensure('positions', { strength: 'strong', force: true })

    expect(strong).not.toBe(weak)
    expect(positions).toHaveBeenCalledTimes(2)
    expect(positions.mock.calls.map((call) => call[1])).toEqual([false, true])

    weakGate.resolve(positionPage(1))
    strongGate.resolve(positionPage(2))
    await Promise.all([weak, strong])
  })

  it('lets a weak request join an existing strong flight', async () => {
    const gate = deferred<PositionPageDTO>()
    const positions = vi.spyOn(api, 'positions').mockReturnValue(gate.promise)
    const resources = new MiniAppResources(AUTH, 'scope-a')

    const strong = resources.ensure('positions', { strength: 'strong', force: true })
    const weak = resources.ensure('positions', { strength: 'weak', force: true })

    expect(weak).toBe(strong)
    expect(positions).toHaveBeenCalledTimes(1)
    expect(positions.mock.calls[0]?.[1]).toBe(true)

    gate.resolve(positionPage(2))
    await strong
  })

  it('keeps a strong result when an older weak flight resolves later', async () => {
    const weakGate = deferred<PositionPageDTO>()
    const strongGate = deferred<PositionPageDTO>()
    vi.spyOn(api, 'positions')
      .mockReturnValueOnce(weakGate.promise)
      .mockReturnValueOnce(strongGate.promise)
    const resources = new MiniAppResources(AUTH, 'scope-a')

    const weak = resources.ensure('positions', { strength: 'weak', force: true })
    const strong = resources.ensure('positions', { strength: 'strong', force: true })

    strongGate.resolve(positionPage(2))
    await strong
    expect(resources.getSnapshot('positions').data?.count).toBe(2)
    expect(resources.getSnapshot('positions').strength).toBe('strong')

    weakGate.resolve(positionPage(1))
    await weak
    await flush()
    expect(resources.getSnapshot('positions').data?.count).toBe(2)
    expect(resources.getSnapshot('positions').strength).toBe('strong')
  })

  it('accepts the older weak result when the newer strong flight fails', async () => {
    const weakGate = deferred<PositionPageDTO>()
    const strongGate = deferred<PositionPageDTO>()
    vi.spyOn(api, 'positions')
      .mockReturnValueOnce(weakGate.promise)
      .mockReturnValueOnce(strongGate.promise)
    const resources = new MiniAppResources(AUTH, 'scope-a')

    const weak = resources.ensure('positions', { strength: 'weak', force: true })
    const strong = resources.ensure('positions', { strength: 'strong', force: true })

    strongGate.reject(new ApiError(503, 'network_error', 'down'))
    await expect(strong).rejects.toMatchObject({ code: 'network_error' })
    weakGate.resolve(positionPage(1))
    await weak
    await flush()

    const snapshot = resources.getSnapshot('positions')
    expect(snapshot.data?.count).toBe(1)
    expect(snapshot.strength).toBe('weak')
    expect(snapshot.error).toBeNull()
    expect(snapshot.stale).toBe(false)
  })

  it('fences a pre-mutation read after publishConfirmed', async () => {
    const gate = deferred<PositionPageDTO>()
    vi.spyOn(api, 'positions').mockReturnValue(gate.promise)
    const resources = new MiniAppResources(AUTH, 'scope-a')

    const pending = resources.ensure('positions', { strength: 'weak', force: true })
    resources.publishConfirmed('positions', positionPage(9))

    gate.resolve(positionPage(1))
    await pending
    await flush()

    const snapshot = resources.getSnapshot('positions')
    expect(snapshot.data?.count).toBe(9)
    expect(snapshot.strength).toBe('strong')
    expect(snapshot.stale).toBe(false)
  })

  it('keeps invalidation during a flight stale and joins exactly one later ensure', async () => {
    const firstGate = deferred<PositionPageDTO>()
    const followupGate = deferred<PositionPageDTO>()
    const positions = vi
      .spyOn(api, 'positions')
      .mockReturnValueOnce(firstGate.promise)
      .mockReturnValueOnce(followupGate.promise)
    const resources = new MiniAppResources(AUTH, 'scope-a')

    const first = resources.ensure('positions', { strength: 'weak', force: true })
    resources.invalidate(['positions'])
    firstGate.resolve(positionPage(1))
    await first
    await flush()

    expect(resources.getSnapshot('positions').data).toBeNull()
    expect(resources.getSnapshot('positions').stale).toBe(true)

    const retryA = resources.ensure('positions', { strength: 'weak' })
    const retryB = resources.ensure('positions', { strength: 'weak' })
    expect(retryB).toBe(retryA)
    expect(positions).toHaveBeenCalledTimes(2)

    followupGate.resolve(positionPage(2))
    await retryA
    await flush()
    expect(resources.getSnapshot('positions').data?.count).toBe(2)
    expect(resources.getSnapshot('positions').stale).toBe(false)
    expect(positions).toHaveBeenCalledTimes(2)
  })

  it('refreshCurrent performs one post-invalidation follow-up after joining an old flight', async () => {
    const oldGate = deferred<PositionPageDTO>()
    const currentGate = deferred<PositionPageDTO>()
    const positions = vi
      .spyOn(api, 'positions')
      .mockReturnValueOnce(oldGate.promise)
      .mockReturnValueOnce(currentGate.promise)
    const resources = new MiniAppResources(AUTH, 'scope-a')

    const old = resources.refresh('positions', 'strong')
    resources.invalidate(['positions'])
    const current = resources.refreshCurrent('positions', 'strong')
    expect(positions).toHaveBeenCalledTimes(1)

    oldGate.resolve(positionPage(1))
    await old
    await flush()
    expect(positions).toHaveBeenCalledTimes(2)
    expect(resources.getSnapshot('positions').stale).toBe(true)

    currentGate.resolve(positionPage(2))
    await expect(current).resolves.toMatchObject({ count: 2 })
    expect(resources.getSnapshot('positions').data?.count).toBe(2)
    expect(resources.getSnapshot('positions').stale).toBe(false)
  })

  it('refreshCurrent follows a rejected fenced flight once but never retries a current failure', async () => {
    const oldGate = deferred<PositionPageDTO>()
    const followupGate = deferred<PositionPageDTO>()
    const positions = vi
      .spyOn(api, 'positions')
      .mockReturnValueOnce(oldGate.promise)
      .mockReturnValueOnce(followupGate.promise)
    const resources = new MiniAppResources(AUTH, 'scope-fenced-reject')

    const old = resources.refresh('positions', 'strong')
    resources.invalidate(['positions'])
    const current = resources.refreshCurrent('positions', 'strong')
    oldGate.reject(new ApiError(0, 'network_error', 'obsolete read failed'))

    await expect(old).rejects.toMatchObject({ code: 'network_error' })
    await flush()
    expect(positions).toHaveBeenCalledTimes(2)
    expect(resources.getSnapshot('positions').error).toBeNull()
    expect(resources.getSnapshot('positions').stale).toBe(true)

    followupGate.resolve(positionPage(2))
    await expect(current).resolves.toMatchObject({ count: 2 })
    expect(resources.getSnapshot('positions').stale).toBe(false)

    const failure = new ApiError(0, 'network_error', 'current read failed')
    positions.mockRejectedValueOnce(failure)
    await expect(resources.refreshCurrent('positions', 'strong')).rejects.toBe(failure)
    await flush()
    expect(positions).toHaveBeenCalledTimes(3)
    expect(resources.getSnapshot('positions').error).toBe('network_error')
  })

  it('dispose aborts the flight signal and fences a late result', async () => {
    const gate = deferred<PositionPageDTO>()
    let signal: AbortSignal | undefined
    vi.spyOn(api, 'positions').mockImplementation((_auth, _refresh, options) => {
      signal = options?.signal
      return gate.promise
    })
    const resources = new MiniAppResources(AUTH, 'scope-a')

    const pending = resources.ensure('positions', { strength: 'weak', force: true })
    expect(signal?.aborted).toBe(false)
    resources.dispose()
    expect(signal?.aborted).toBe(true)
    expect(resources.getSnapshot('positions').data).toBeNull()

    gate.resolve(positionPage(7))
    await pending
    await flush()
    expect(resources.getSnapshot('positions').data).toBeNull()
    expect(resources.getSnapshot('positions').stale).toBe(true)
    await expect(resources.ensure('positions')).rejects.toMatchObject({ code: 'aborted' })
  })
})

describe('MiniAppResources mutation locks', () => {
  it('keeps a mutation locked until the acquired release is called', () => {
    const resources = new MiniAppResources(AUTH, 'scope-a')

    const release = resources.acquireMutation('pause-resume')
    expect(release).not.toBeNull()
    expect(resources.mutationLocked('pause-resume')).toBe(true)
    expect(resources.acquireMutation('pause-resume')).toBeNull()

    release?.()
    expect(resources.mutationLocked('pause-resume')).toBe(false)

    const releaseAgain = resources.acquireMutation('pause-resume')
    expect(releaseAgain).not.toBeNull()
    releaseAgain?.()
  })

  it('retains unknown outcome, never resolves an active flight, and fences stale release closures', () => {
    const resources = new MiniAppResources(AUTH, 'scope-unknown-lock')
    const firstRelease = resources.acquireMutation('pause-resume')
    expect(firstRelease).not.toBeNull()
    expect(resources.mutationState('pause-resume')).toBe('active')

    // Reconciliation is allowed to clear only retained uncertainty, never an
    // operation that is still genuinely in flight.
    expect(resources.resolveUnknownMutation('pause-resume')).toBe(false)
    expect(resources.mutationState('pause-resume')).toBe('active')

    expect(resources.markMutationUnknown('pause-resume', 'pause')).toBe(true)
    expect(resources.mutationState('pause-resume')).toBe('unknown')
    expect(resources.mutationUnknownReason('pause-resume')).toBe('pause')

    // The old operation's finally/release cannot erase retained uncertainty.
    firstRelease?.()
    expect(resources.mutationState('pause-resume')).toBe('unknown')

    expect(resources.resolveUnknownMutation('pause-resume')).toBe(true)
    expect(resources.mutationState('pause-resume')).toBeNull()

    const secondRelease = resources.acquireMutation('pause-resume')
    expect(secondRelease).not.toBeNull()
    firstRelease?.()
    expect(resources.mutationState('pause-resume')).toBe('active')
    secondRelease?.()
    expect(resources.mutationState('pause-resume')).toBeNull()
  })
})

describe('MiniAppResources scope truth', () => {
  it('handles a direct invalid-session error through the owner exactly once', () => {
    const invalid = vi.fn()
    const resources = new MiniAppResources(AUTH, 'scope-direct-auth', invalid)

    expect(resources.handleInvalidSessionError(new ApiError(401, 'unauthorized', 'expired'))).toBe(true)
    expect(resources.handleInvalidSessionError(new ApiError(500, 'invalid_session', 'expired'))).toBe(true)
    expect(invalid).toHaveBeenCalledTimes(1)
    expect(resources.getSnapshot('positions').data).toBeNull()
  })

  it('treats a resource 401 as a hard session boundary and reports it once', async () => {
    vi.spyOn(api, 'positions').mockRejectedValue(
      new ApiError(401, 'invalid_session', 'expired'),
    )
    const invalid = vi.fn()
    const resources = new MiniAppResources(AUTH, 'scope-auth', invalid)

    await expect(
      resources.ensure('positions', { strength: 'weak', force: true }),
    ).rejects.toMatchObject({ status: 401, code: 'invalid_session' })
    await flush()

    expect(invalid).toHaveBeenCalledTimes(1)
    expect(resources.getSnapshot('positions').data).toBeNull()
    await expect(resources.ensure('positions')).rejects.toMatchObject({ code: 'aborted' })
  })

  it('keeps cached data visible but marks it stale as soon as a required refresh starts', async () => {
    const gate = deferred<PositionPageDTO>()
    vi.spyOn(api, 'positions').mockReturnValue(gate.promise)
    const resources = new MiniAppResources(AUTH, 'scope-refresh-stale')
    resources.publishConfirmed('positions', positionPage(1))

    const pending = resources.refresh('positions', 'strong')
    const pendingSnapshot = resources.getSnapshot('positions')
    expect(pendingSnapshot.data?.count).toBe(1)
    expect(pendingSnapshot.stale).toBe(true)
    expect(pendingSnapshot.inflight).toBe(true)

    gate.resolve(positionPage(2))
    await pending
    expect(resources.getSnapshot('positions').data?.count).toBe(2)
    expect(resources.getSnapshot('positions').stale).toBe(false)
  })

  it('50 connected-scope replacements abort every old flight and publish no late DTO', async () => {
    const gates: ReturnType<typeof deferred<PositionPageDTO>>[] = []
    const signals: AbortSignal[] = []
    vi.spyOn(api, 'positions').mockImplementation((_auth, _refresh, options) => {
      const gate = deferred<PositionPageDTO>()
      gates.push(gate)
      if (options?.signal) signals.push(options.signal)
      return gate.promise
    })

    const owners: MiniAppResources[] = []
    const pending: Promise<PositionPageDTO>[] = []
    for (let i = 0; i < 50; i += 1) {
      const owner = new MiniAppResources(AUTH, `scope-${i}`)
      owners.push(owner)
      pending.push(owner.ensure('positions', { strength: 'weak', force: true }))
      owner.dispose()
    }

    expect(signals).toHaveLength(50)
    expect(signals.every((signal) => signal.aborted)).toBe(true)
    gates.forEach((gate, index) => gate.resolve(positionPage(index + 1)))
    await Promise.all(pending)
    await flush()

    for (const owner of owners) {
      const snapshot = owner.getSnapshot('positions')
      expect(snapshot.data).toBeNull()
      expect(snapshot.inflight).toBe(false)
    }
  })
})
