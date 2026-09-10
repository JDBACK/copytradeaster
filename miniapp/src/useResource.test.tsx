// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest'
import { StrictMode } from 'react'
import { act } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { api, ApiError } from './api'
import type { LiveUpdate } from './live'
import { MiniAppResources } from './resources'
import type { DashboardDTO, PositionPageDTO } from './types'
import { useResource } from './useResource'

const AUTH = { initData: 'signed-test' }

function positionPage(count: number): PositionPageDTO {
  return {
    positions: [],
    count,
    system_state: 'ready',
    snapshot_captured_at: `2026-09-07T12:00:${String(count).padStart(2, '0')}+00:00`,
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

function Probe({
  resources,
  name,
  active = true,
  foregroundSignal = 0,
}: {
  resources: MiniAppResources
  name: string
  active?: boolean
  foregroundSignal?: number
}) {
  const { snapshot } = useResource({
    resources,
    key: 'positions',
    active,
    foregroundSignal,
  })
  return (
    <div data-testid={name}>
      {snapshot.data ? `ready:${snapshot.data.count}` : snapshot.error ? `error:${snapshot.error}` : 'loading'}
    </div>
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function liveUpdate(resources: MiniAppResources, event: LiveUpdate): void {
  ;(resources as unknown as { applyLiveUpdate: (update: LiveUpdate) => void }).applyLiveUpdate(event)
}

describe('useResource lifecycle', () => {
  it('reaches ready in StrictMode with one shared initial API call', async () => {
    const gate = deferred<PositionPageDTO>()
    const positions = vi.spyOn(api, 'positions').mockReturnValue(gate.promise)
    const resources = new MiniAppResources(AUTH, 'scope-strict')

    render(
      <StrictMode>
        <Probe resources={resources} name="probe" />
      </StrictMode>,
    )

    await waitFor(() => expect(positions).toHaveBeenCalledTimes(1))
    gate.resolve(positionPage(1))

    expect(await screen.findByText('ready:1')).toBeInTheDocument()
    expect(positions).toHaveBeenCalledTimes(1)
  })

  it('shares one owner flight across two consumers and does not abort it when one unmounts', async () => {
    const gate = deferred<PositionPageDTO>()
    let signal: AbortSignal | undefined
    const positions = vi.spyOn(api, 'positions').mockImplementation((_auth, _refresh, options) => {
      signal = options?.signal
      return gate.promise
    })
    const resources = new MiniAppResources(AUTH, 'scope-shared')

    const { rerender } = render(
      <>
        <Probe resources={resources} name="first" />
        <Probe resources={resources} name="second" />
      </>,
    )

    await waitFor(() => expect(positions).toHaveBeenCalledTimes(1))
    expect(signal?.aborted).toBe(false)

    rerender(<Probe resources={resources} name="second" />)
    expect(screen.queryByTestId('first')).toBeNull()
    expect(signal?.aborted).toBe(false)

    gate.resolve(positionPage(2))
    expect(await screen.findByText('ready:2')).toBeInTheDocument()
    expect(positions).toHaveBeenCalledTimes(1)
    expect(signal?.aborted).toBe(false)
  })

  it('releases a screen-owned flight after the last consumer unmounts', async () => {
    const gate = deferred<PositionPageDTO>()
    let signal: AbortSignal | undefined
    vi.spyOn(api, 'positions').mockImplementation((_auth, _refresh, options) => {
      signal = options?.signal
      return gate.promise
    })
    const resources = new MiniAppResources(AUTH, 'scope-last-consumer')
    const view = render(<Probe resources={resources} name="probe" />)
    await waitFor(() => expect(signal).toBeDefined())
    expect(signal?.aborted).toBe(false)

    view.unmount()
    await Promise.resolve()
    expect(signal?.aborted).toBe(true)
    gate.resolve(positionPage(1))
  })

  it('does not release a mutation-protected reconciliation flight on unmount', async () => {
    const gate = deferred<PositionPageDTO>()
    let signal: AbortSignal | undefined
    vi.spyOn(api, 'positions').mockImplementation((_auth, _refresh, options) => {
      signal = options?.signal
      return gate.promise
    })
    const resources = new MiniAppResources(AUTH, 'scope-protected-consumer')
    // Positions is not mutation-owned, so use Dashboard's mutation relationship
    // through a direct subscription/read to exercise the protection contract.
    const dashboardGate = deferred<DashboardDTO>()
    let dashboardSignal: AbortSignal | undefined
    vi.spyOn(api, 'dashboard').mockImplementation((_auth, _refresh, options) => {
      dashboardSignal = options?.signal
      return dashboardGate.promise
    })
    const release = resources.acquireMutation('pause-resume')
    const unsubscribe = resources.subscribe('dashboard', () => {})
    void resources.ensure('dashboard', { strength: 'strong', force: true }).catch(() => {})
    await Promise.resolve()
    unsubscribe()
    await Promise.resolve()
    expect(dashboardSignal?.aborted).toBe(false)
    release?.()
    resources.cancelUnobservedRead('dashboard', 'strong')
    expect(dashboardSignal?.aborted).toBe(true)
    dashboardGate.resolve({
      copy_state: 'active',
      system_state: 'ready',
      notification_enabled: true,
      is_admin: false,
    })
    gate.resolve(positionPage(1))
    void signal
  })

  it('performs exactly one follow-up after active invalidation lands during an in-flight read', async () => {
    const first = deferred<PositionPageDTO>()
    const followup = deferred<PositionPageDTO>()
    const positions = vi
      .spyOn(api, 'positions')
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(followup.promise)
    const resources = new MiniAppResources(AUTH, 'scope-invalidate')

    render(<Probe resources={resources} name="probe" />)
    await waitFor(() => expect(positions).toHaveBeenCalledTimes(1))

    resources.invalidate(['positions'])
    expect(positions).toHaveBeenCalledTimes(1)

    first.resolve(positionPage(1))
    await waitFor(() => expect(positions).toHaveBeenCalledTimes(2))
    expect(screen.getByTestId('probe')).toHaveTextContent('loading')

    followup.resolve(positionPage(2))
    expect(await screen.findByText('ready:2')).toBeInTheDocument()
    expect(positions).toHaveBeenCalledTimes(2)
  })

  it('does not self-loop after a failed read and retries only on reactivation', async () => {
    const positions = vi
      .spyOn(api, 'positions')
      .mockRejectedValueOnce(new ApiError(0, 'network_error', 'down'))
      .mockResolvedValueOnce(positionPage(3))
    const resources = new MiniAppResources(AUTH, 'scope-retry')

    const { rerender } = render(<Probe resources={resources} name="probe" active />)

    expect(await screen.findByText('error:network_error')).toBeInTheDocument()
    await Promise.resolve()
    expect(positions).toHaveBeenCalledTimes(1)

    rerender(<Probe resources={resources} name="probe" active={false} />)
    expect(positions).toHaveBeenCalledTimes(1)
    rerender(<Probe resources={resources} name="probe" active />)

    expect(await screen.findByText('ready:3')).toBeInTheDocument()
    expect(positions).toHaveBeenCalledTimes(2)
  })

  it('retries a failed read on a new foreground signal without self-looping', async () => {
    const positions = vi
      .spyOn(api, 'positions')
      .mockRejectedValueOnce(new ApiError(0, 'network_error', 'down'))
      .mockResolvedValueOnce(positionPage(4))
    const resources = new MiniAppResources(AUTH, 'scope-foreground')

    const { rerender } = render(
      <Probe resources={resources} name="probe" active foregroundSignal={0} />,
    )

    expect(await screen.findByText('error:network_error')).toBeInTheDocument()
    await Promise.resolve()
    expect(positions).toHaveBeenCalledTimes(1)

    rerender(<Probe resources={resources} name="probe" active foregroundSignal={1} />)
    expect(await screen.findByText('ready:4')).toBeInTheDocument()
    expect(positions).toHaveBeenCalledTimes(2)
  })

  it('widens the self-heal delay while failures repeat', async () => {
    vi.useFakeTimers()
    try {
      const positions = vi
        .spyOn(api, 'positions')
        .mockRejectedValue(new ApiError(0, 'network_error', 'down'))
      const resources = new MiniAppResources(AUTH, 'scope-self-heal-ladder')
      render(<Probe resources={resources} name="probe" active />)

      await act(async () => {})
      expect(positions).toHaveBeenCalledTimes(1)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000)
      })
      expect(positions).toHaveBeenCalledTimes(2)

      // The second failure must wait the widened 2s step: reseting the ladder
      // whenever the error slot cleared kept every retry at 1s, which turned a
      // sustained outage into a ~1 Hz re-read loop instead of a backoff.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000)
      })
      expect(positions).toHaveBeenCalledTimes(2)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000)
      })
      expect(positions).toHaveBeenCalledTimes(3)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a hidden keep-alive consumer is not re-read by a live invalidation', async () => {
    const positions = vi.spyOn(api, 'positions').mockResolvedValue(positionPage(5))
    const resources = new MiniAppResources(AUTH, 'scope-hidden-live')
    const { rerender } = render(<Probe resources={resources} name="probe" active={false} />)
    await waitFor(() => expect(positions).toHaveBeenCalledTimes(1))

    liveUpdate(resources, { resources: ['positions'], revision: 1, source: 'rest' })
    await act(async () => {})
    // Still hidden: the live event must not spend a read on an invisible tab,
    // and activation is what revalidates it.
    expect(positions).toHaveBeenCalledTimes(1)

    rerender(<Probe resources={resources} name="probe" active />)
    await waitFor(() => expect(positions).toHaveBeenCalledTimes(2))
  })


  it('backs off repeated activation retries after consecutive failures', async () => {
    let now = 10_000
    vi.spyOn(Date, 'now').mockImplementation(() => now)
    const positions = vi
      .spyOn(api, 'positions')
      .mockRejectedValue(new ApiError(0, 'network_error', 'down'))
    const resources = new MiniAppResources(AUTH, 'scope-retry-backoff')
    const { rerender } = render(<Probe resources={resources} name="probe" active />)

    expect(await screen.findByText('error:network_error')).toBeInTheDocument()
    expect(positions).toHaveBeenCalledTimes(1)

    rerender(<Probe resources={resources} name="probe" active={false} />)
    rerender(<Probe resources={resources} name="probe" active />)
    await waitFor(() => expect(positions).toHaveBeenCalledTimes(2))

    now += 500
    rerender(<Probe resources={resources} name="probe" active={false} />)
    rerender(<Probe resources={resources} name="probe" active />)
    await Promise.resolve()
    expect(positions).toHaveBeenCalledTimes(2)

    now += 600
    rerender(<Probe resources={resources} name="probe" active={false} />)
    rerender(<Probe resources={resources} name="probe" active />)
    await waitFor(() => expect(positions).toHaveBeenCalledTimes(3))
  })

  it('does not self-loop a failed stale refresh and retries only after reactivation', async () => {
    const positions = vi
      .spyOn(api, 'positions')
      .mockResolvedValueOnce(positionPage(1))
      .mockRejectedValueOnce(new ApiError(0, 'network_error', 'down'))
      .mockResolvedValueOnce(positionPage(2))
    const resources = new MiniAppResources(AUTH, 'scope-stale-retry')

    const { rerender } = render(<Probe resources={resources} name="probe" active />)
    expect(await screen.findByText('ready:1')).toBeInTheDocument()
    expect(positions).toHaveBeenCalledTimes(1)

    resources.invalidate(['positions'])
    await waitFor(() => expect(positions).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(resources.getSnapshot('positions').error).toBe('network_error'))
    await Promise.resolve()
    await Promise.resolve()
    expect(positions).toHaveBeenCalledTimes(2)
    expect(screen.getByTestId('probe')).toHaveTextContent('ready:1')

    rerender(<Probe resources={resources} name="probe" active={false} />)
    rerender(<Probe resources={resources} name="probe" active />)
    expect(await screen.findByText('ready:2')).toBeInTheDocument()
    expect(positions).toHaveBeenCalledTimes(3)
  })

  it('dispose aborts the owner flight and fences a late publication', async () => {
    const gate = deferred<PositionPageDTO>()
    let signal: AbortSignal | undefined
    const positions = vi.spyOn(api, 'positions').mockImplementation((_auth, _refresh, options) => {
      signal = options?.signal
      return gate.promise
    })
    const resources = new MiniAppResources(AUTH, 'scope-dispose')

    render(<Probe resources={resources} name="probe" />)
    await waitFor(() => expect(positions).toHaveBeenCalledTimes(1))
    expect(signal?.aborted).toBe(false)

    resources.dispose()
    expect(signal?.aborted).toBe(true)

    gate.resolve(positionPage(9))
    await Promise.resolve()
    await Promise.resolve()

    expect(resources.getSnapshot('positions').data).toBeNull()
    expect(screen.getByTestId('probe')).not.toHaveTextContent('ready:9')
    expect(positions).toHaveBeenCalledTimes(1)
  })
})
