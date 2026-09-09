import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from './api'
import { MiniAppResources } from './resources'
import { SETTINGS_PREFETCH_KEYS, createSettingsPrefetch } from './settingsPrefetch'
import type {
  AccountBuilderDTO,
  NotificationSettingsDTO,
  RiskDTO,
  StatusDTO,
} from './types'

const AUTH = { initData: 'signed-test' }

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
  await Promise.resolve()
}

function risk(): RiskDTO {
  return {
    risk_percent: '0.01',
    min: '0.005',
    max: '0.05',
    step: '0.005',
    presets: ['0.01', '0.02'],
    risk_base: 'available_balance',
  }
}

function notifications(): NotificationSettingsDTO {
  return { enabled: true, pending: 0, failed: 0, last_delivery_at: null }
}

function status(): StatusDTO {
  return {
    state: 'ready',
    system_state: 'ready',
    copy_state: 'paused',
    aster_state: 'ok',
    permissions_state: 'valid',
    permissions: {},
    reason: 'none',
    remediation: null,
  }
}

function accountBuilder(): AccountBuilderDTO {
  return {
    identity_source: 'builder',
    state: 'verified',
    reason: 'none',
    remediation: null,
    permission: { can_read: true, can_perp_trade: true, can_withdraw: false },
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('SettingsPrefetch', () => {
  it('uses the fixed order and never runs more than two speculative reads', async () => {
    expect(SETTINGS_PREFETCH_KEYS).toEqual(['risk', 'notifications', 'status', 'accountBuilder'])

    const riskGate = deferred<RiskDTO>()
    const notificationsGate = deferred<NotificationSettingsDTO>()
    const statusGate = deferred<StatusDTO>()
    const accountGate = deferred<AccountBuilderDTO>()
    const started: string[] = []
    let active = 0
    let peak = 0
    const track = <T>(key: string, promise: Promise<T>) => {
      started.push(key)
      active += 1
      peak = Math.max(peak, active)
      void promise.finally(() => {
        active -= 1
      })
      return promise
    }

    vi.spyOn(api, 'risk').mockImplementation(() => track('risk', riskGate.promise))
    vi.spyOn(api, 'notifications').mockImplementation(() =>
      track('notifications', notificationsGate.promise),
    )
    const statusSpy = vi
      .spyOn(api, 'status')
      .mockImplementation((_auth, _refresh) => track('status', statusGate.promise))
    const accountSpy = vi
      .spyOn(api, 'accountBuilder')
      .mockImplementation((_auth, _refresh) => track('accountBuilder', accountGate.promise))

    const prefetch = createSettingsPrefetch(new MiniAppResources(AUTH, 'scope-a'))
    prefetch.start()
    prefetch.start()
    expect(started).toEqual(['risk', 'notifications'])

    riskGate.resolve(risk())
    await flush()
    expect(started).toEqual(['risk', 'notifications', 'status'])

    notificationsGate.resolve(notifications())
    await flush()
    expect(started).toEqual(['risk', 'notifications', 'status', 'accountBuilder'])
    expect(peak).toBe(2)
    expect(statusSpy).toHaveBeenCalledWith(AUTH, false, expect.any(Object))
    expect(accountSpy).toHaveBeenCalledWith(AUTH, false, expect.any(Object))

    statusGate.resolve(status())
    accountGate.resolve(accountBuilder())
    await flush()
  })

  it('stop discards queued tail and releases already-started unobserved prefetch flights', async () => {
    const riskGate = deferred<RiskDTO>()
    const notificationsGate = deferred<NotificationSettingsDTO>()
    let riskSignal: AbortSignal | undefined
    let notificationsSignal: AbortSignal | undefined
    vi.spyOn(api, 'risk').mockImplementation((_auth, options) => {
      riskSignal = options?.signal
      return riskGate.promise
    })
    vi.spyOn(api, 'notifications').mockImplementation((_auth, options) => {
      notificationsSignal = options?.signal
      return notificationsGate.promise
    })
    const statusSpy = vi.spyOn(api, 'status').mockResolvedValue(status())
    const accountSpy = vi.spyOn(api, 'accountBuilder').mockResolvedValue(accountBuilder())

    const prefetch = createSettingsPrefetch(new MiniAppResources(AUTH, 'scope-a'))
    prefetch.start()
    prefetch.stop()

    expect(riskSignal?.aborted).toBe(true)
    expect(notificationsSignal?.aborted).toBe(true)
    riskGate.resolve(risk())
    notificationsGate.resolve(notifications())
    await flush()
    expect(statusSpy).not.toHaveBeenCalled()
    expect(accountSpy).not.toHaveBeenCalled()
  })

  it('stop never aborts a running weak flight once a real consumer subscribed', async () => {
    const resources = new MiniAppResources(AUTH, 'scope-consumer')
    const riskGate = deferred<RiskDTO>()
    let riskSignal: AbortSignal | undefined
    vi.spyOn(api, 'risk').mockImplementation((_auth, options) => {
      riskSignal = options?.signal
      return riskGate.promise
    })
    vi.spyOn(api, 'notifications').mockResolvedValue(notifications())
    vi.spyOn(api, 'status').mockResolvedValue(status())
    vi.spyOn(api, 'accountBuilder').mockResolvedValue(accountBuilder())
    const unsubscribe = resources.subscribe('risk', () => {})

    const prefetch = createSettingsPrefetch(resources)
    prefetch.start()
    prefetch.stop()

    expect(riskSignal?.aborted).toBe(false)
    riskGate.resolve(risk())
    await flush()
    unsubscribe()
  })

  it('prioritize replaces unobserved speculative tail so the user target starts without a third slot', async () => {
    const riskGate = deferred<RiskDTO>()
    const notificationsGate = deferred<NotificationSettingsDTO>()
    const accountGate = deferred<AccountBuilderDTO>()
    const started: string[] = []
    vi.spyOn(api, 'risk').mockImplementation(() => {
      started.push('risk')
      return riskGate.promise
    })
    vi.spyOn(api, 'notifications').mockImplementation(() => {
      started.push('notifications')
      return notificationsGate.promise
    })
    vi.spyOn(api, 'status').mockImplementation(() => {
      started.push('status')
      return Promise.resolve(status())
    })
    vi.spyOn(api, 'accountBuilder').mockImplementation(() => {
      started.push('accountBuilder')
      return accountGate.promise
    })

    const prefetch = createSettingsPrefetch(new MiniAppResources(AUTH, 'scope-a'))
    prefetch.start()
    prefetch.prioritize('accountBuilder')
    expect(started).toEqual(['risk', 'notifications', 'accountBuilder'])

    riskGate.resolve(risk())
    await flush()
    expect(started).toEqual(['risk', 'notifications', 'accountBuilder', 'status'])

    notificationsGate.resolve(notifications())
    accountGate.resolve(accountBuilder())
    await flush()
  })

  it('prioritize joins running work and skips a fresh resource without duplicate reads', async () => {
    const resources = new MiniAppResources(AUTH, 'scope-a')
    const riskGate = deferred<RiskDTO>()
    const riskSpy = vi.spyOn(api, 'risk').mockReturnValue(riskGate.promise)
    const notificationsSpy = vi.spyOn(api, 'notifications').mockResolvedValue(notifications())
    vi.spyOn(api, 'status').mockResolvedValue(status())
    vi.spyOn(api, 'accountBuilder').mockResolvedValue(accountBuilder())

    const prefetch = createSettingsPrefetch(resources)
    prefetch.start()
    prefetch.prioritize('risk')
    prefetch.prioritize('risk')
    expect(riskSpy).toHaveBeenCalledTimes(1)

    riskGate.resolve(risk())
    await flush()
    prefetch.prioritize('risk')
    expect(riskSpy).toHaveBeenCalledTimes(1)

    await flush()
    expect(notificationsSpy).toHaveBeenCalledTimes(1)
  })

  it('demotes a user target after the route-transition stop so a later leave can cancel it', async () => {
    const resources = new MiniAppResources(AUTH, 'scope-target-demotion')
    const riskGate = deferred<RiskDTO>()
    let riskSignal: AbortSignal | undefined
    vi.spyOn(api, 'risk').mockImplementation((_auth, options) => {
      riskSignal = options?.signal
      return riskGate.promise
    })
    vi.spyOn(api, 'notifications').mockReturnValue(new Promise(() => {}))
    vi.spyOn(api, 'status').mockResolvedValue(status())
    vi.spyOn(api, 'accountBuilder').mockResolvedValue(accountBuilder())

    const prefetch = createSettingsPrefetch(resources)
    prefetch.start()
    prefetch.prioritize('risk')
    prefetch.stop()
    expect(riskSignal?.aborted).toBe(false)

    const unsubscribe = resources.subscribe('risk', () => {})
    await flush()
    unsubscribe()
    await flush()

    // Simulate Back to Settings and a later tab leave while the old target is
    // still unresolved and has no mounted consumer.
    prefetch.start()
    prefetch.stop()
    expect(riskSignal?.aborted).toBe(true)
    riskGate.resolve(risk())
    await flush()
  })

  it('prioritizing an already-running Status weak flight yields the other speculative slot', async () => {
    const resources = new MiniAppResources(AUTH, 'scope-status-priority')
    const riskGate = deferred<RiskDTO>()
    const notificationsGate = deferred<NotificationSettingsDTO>()
    const statusGate = deferred<StatusDTO>()
    let notificationsSignal: AbortSignal | undefined
    let statusSignal: AbortSignal | undefined
    vi.spyOn(api, 'risk').mockReturnValue(riskGate.promise)
    vi.spyOn(api, 'notifications').mockImplementation((_auth, options) => {
      notificationsSignal = options?.signal
      return notificationsGate.promise
    })
    vi.spyOn(api, 'status').mockImplementation((_auth, _refresh, options) => {
      statusSignal = options?.signal
      return statusGate.promise
    })
    vi.spyOn(api, 'accountBuilder').mockResolvedValue(accountBuilder())

    const prefetch = createSettingsPrefetch(resources)
    prefetch.start()
    riskGate.resolve(risk())
    await flush()
    expect(statusSignal?.aborted).toBe(false)
    expect(notificationsSignal?.aborted).toBe(false)

    prefetch.prioritize('status')
    expect(statusSignal?.aborted).toBe(false)
    expect(notificationsSignal?.aborted).toBe(true)

    statusGate.resolve(status())
    notificationsGate.resolve(notifications())
    await flush()
  })

  it('rewarms a target canceled by last-consumer Back during the same visible Settings pass', async () => {
    const resources = new MiniAppResources(AUTH, 'scope-back-rewarm')
    let riskCalls = 0
    const secondRisk = deferred<RiskDTO>()
    vi.spyOn(api, 'risk').mockImplementation((_auth, options) => {
      riskCalls += 1
      if (riskCalls === 2) return secondRisk.promise
      return new Promise<RiskDTO>((_resolve, reject) => {
        options?.signal?.addEventListener('abort', () => reject(new Error('aborted')), { once: true })
      })
    })
    const notificationsGate = deferred<NotificationSettingsDTO>()
    vi.spyOn(api, 'notifications').mockReturnValue(notificationsGate.promise)
    vi.spyOn(api, 'status').mockResolvedValue(status())
    vi.spyOn(api, 'accountBuilder').mockResolvedValue(accountBuilder())

    const prefetch = createSettingsPrefetch(resources)
    prefetch.start()
    prefetch.prioritize('risk')
    prefetch.stop()
    const unsubscribe = resources.subscribe('risk', () => {})
    await flush()

    // Back makes Settings visible before the old target has settled. start()
    // initially excludes that running key; last-consumer cancellation must put
    // it back into this same pass rather than losing the warmup opportunity.
    prefetch.start()
    unsubscribe()
    await flush()
    expect(riskCalls).toBe(2)

    secondRisk.resolve(risk())
    notificationsGate.resolve(notifications())
    await flush()
  })
})
