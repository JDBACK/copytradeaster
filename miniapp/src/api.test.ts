import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, type AuthContext } from './api'

const auth: AuthContext = { initData: 'signed-data', devUserId: 42 }

const sessionDto = {
  user_id: 42,
  access_state: 'approved',
  environment: 'testnet',
  account_connected: true,
  connection_state: 'connected',
} as const

const dashboardDto = {
  copy_state: 'paused',
  system_state: 'ready',
  notification_enabled: true,
  is_admin: false,
} as const

const onboardedDto = {
  account_id: 'tg-42',
  account_masked: '0xAA…BB',
  signer_masked: '0x11…22',
  builder_name: 'copytradeaster',
  max_fee_rate: '0.0001',
  position_mode: 'ONE_WAY',
  available_usdt: '100',
  agent_expires_at_ms: 2_000_000_000_000,
  paused: true,
} as const

function mockFetch(impl: (url?: string, init?: RequestInit) => Promise<Response>) {
  const fetchMock = vi.fn(impl)
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('api client', () => {
  it('sends initData and dev headers and returns parsed payload', async () => {
    const fetchMock = mockFetch(async () => Response.json(sessionDto))
    const result = await api.session(auth)
    expect(result).toEqual(sessionDto)
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    const headers = init.headers as Record<string, string>
    expect(headers['X-Telegram-Init-Data']).toBe('signed-data')
    expect(headers['X-Dev-User-Id']).toBe('42')
    expect(headers['Content-Type']).toBe('application/json')
  })

  it('omits dev header without devUserId', async () => {
    const fetchMock = mockFetch(async () => Response.json(sessionDto))
    await api.session({ initData: 'x' })
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect((init.headers as Record<string, string>)['X-Dev-User-Id']).toBeUndefined()
  })

  it('maps server error payloads to ApiError with the typed code', async () => {
    mockFetch(async () =>
      Response.json({ error: 'owner_conflict', message: 'уже подключён' }, { status: 400 }),
    )
    const err = await api.session(auth).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).code).toBe('owner_conflict')
    expect((err as ApiError).status).toBe(400)
    expect((err as ApiError).message).toBe('уже подключён')
  })

  it('uses fallback copy when the error body is not JSON', async () => {
    mockFetch(async () => new Response('boom', { status: 500 }))
    const err = (await api.dashboard(auth).catch((e: unknown) => e)) as ApiError
    expect(err.code).toBe('unclassified_error')
  })

  it('fails closed when a successful response body is not JSON', async () => {
    mockFetch(async () => new Response('not-json', { status: 200 }))
    const err = (await api.dashboard(auth).catch((e: unknown) => e)) as ApiError
    expect(err.code).toBe('invalid_response')
    expect(err.status).toBe(200)
  })

  it('fails closed when a successful response body is JSON null', async () => {
    mockFetch(async () => Response.json(null))
    const err = (await api.dashboard(auth).catch((e: unknown) => e)) as ApiError
    expect(err.code).toBe('invalid_response')
  })

  it.each([{}, [], 'ok', 7, true])(
    'fails closed when a successful response has an unexpected JSON shape: %j',
    async (payload) => {
      mockFetch(async () => Response.json(payload))
      const err = (await api.dashboard(auth).catch((e: unknown) => e)) as ApiError
      expect(err.code).toBe('invalid_response')
      expect(err.status).toBe(200)
    },
  )

  it('fails closed on a non-empty foreign object that is not the expected DTO', async () => {
    mockFetch(async () => Response.json({ message: 'oops' }))
    const err = (await api.dashboard(auth).catch((e: unknown) => e)) as ApiError
    expect(err.code).toBe('invalid_response')
    expect(err.status).toBe(200)
  })

  it('fails closed on malformed nested DTO rows and invalid closed enum values', async () => {
    mockFetch(async () =>
      Response.json({ positions: [{}], count: 1, system_state: 'ready', is_stale: false }),
    )
    const positionsErr = (await api.positions(auth).catch((e: unknown) => e)) as ApiError
    expect(positionsErr.code).toBe('invalid_response')

    mockFetch(async () =>
      Response.json({
        ...sessionDto,
        access_state: 'BOGUS',
      }),
    )
    const sessionErr = (await api.session(auth).catch((e: unknown) => e)) as ApiError
    expect(sessionErr.code).toBe('invalid_response')
  })

  it('maps transport failures to network_error', async () => {
    mockFetch(async () => {
      throw new TypeError('offline')
    })
    const err = (await api.pause(auth).catch((e: unknown) => e)) as ApiError
    expect(err.code).toBe('network_error')
    expect(err.status).toBe(0)
  })

  it('sends the confirmation object on resume', async () => {
    const fetchMock = mockFetch(async () => Response.json({ ...dashboardDto, copy_state: 'active' }))
    await api.resume(auth, { token: 'tok', action: 'resume', account_id: 'tg-1' })
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({
      confirmation: { token: 'tok', action: 'resume', account_id: 'tg-1' },
    })
  })

  it('requests a fresh exchange snapshot when dashboard refresh is true', async () => {
    const fetchMock = mockFetch(async () => Response.json(dashboardDto))
    await api.dashboard(auth, true)
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({ refresh: true })
  })

  it('calls builderRecover endpoint correctly', async () => {
    const fetchMock = mockFetch(async () => Response.json(sessionDto))
    const result = await api.builderRecover(auth)
    expect(result.connection_state).toBe('connected')
    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/miniapp/api/builder/recover')
  })

  it('continues a server-owned builder attempt without client signature data', async () => {
    const fetchMock = mockFetch(async () => Response.json(onboardedDto))
    const result = await api.builderResumeAttempt(auth)
    expect(result.account_id).toBe('tg-42')
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/miniapp/api/builder/resume-attempt')
    expect(JSON.parse(String(init.body))).toEqual({})
  })

  it('aborts on timeout and throws timeout_error', async () => {
    vi.useFakeTimers()
    try {
      mockFetch((_url?: string, init?: RequestInit) => {
        let rejectPromise: ((reason?: unknown) => void) | undefined
        const promise = new Promise<Response>((_, reject) => {
          rejectPromise = reject
        })
        const signal = init?.signal
        signal?.addEventListener('abort', () => {
          const err = new Error('aborted')
          err.name = 'AbortError'
          if (rejectPromise) rejectPromise(err)
        })
        return promise
      })
      const pending = api.dashboard(auth, false, { timeoutMs: 100 })
      vi.advanceTimersByTime(150)
      const err = (await pending.catch((e: unknown) => e)) as ApiError
      expect(err.code).toBe('timeout_error')
      expect(err.message).toContain('истекло')
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps the timeout active while the response body is still pending', async () => {
    vi.useFakeTimers()
    try {
      mockFetch(async () => {
        return {
          ok: true,
          status: 200,
          json: () => new Promise<unknown>(() => {}),
        } as Response
      })
      const pending = api.dashboard(auth, false, { timeoutMs: 100 }).catch((e: unknown) => e)
      await vi.advanceTimersByTimeAsync(150)
      const err = (await pending) as ApiError
      expect(err.code).toBe('timeout_error')
    } finally {
      vi.useRealTimers()
    }
  })

  it('removes the caller abort listener after a successful request', async () => {
    const controller = new AbortController()
    const add = vi.spyOn(controller.signal, 'addEventListener')
    const remove = vi.spyOn(controller.signal, 'removeEventListener')
    mockFetch(async () => Response.json(dashboardDto))

    await api.dashboard(auth, false, { signal: controller.signal })

    expect(add).toHaveBeenCalledWith('abort', expect.any(Function), { once: true })
    expect(remove).toHaveBeenCalledWith('abort', expect.any(Function))
  })

  it('preserves timeout classification if the caller aborts after the timeout already won', async () => {
    vi.useFakeTimers()
    try {
      const external = new AbortController()
      mockFetch((_url?: string, init?: RequestInit) => {
        return new Promise<Response>((_, reject) => {
          init?.signal?.addEventListener('abort', () => {
            const error = new Error('aborted')
            error.name = 'AbortError'
            reject(error)
          })
        })
      })
      const pending = api.dashboard(auth, false, { timeoutMs: 100, signal: external.signal })
      vi.advanceTimersByTime(100)
      external.abort()
      const err = (await pending.catch((e: unknown) => e)) as ApiError
      expect(err.code).toBe('timeout_error')
    } finally {
      vi.useRealTimers()
    }
  })

  it('preserves external-abort classification if the timeout fires before delayed rejection', async () => {
    vi.useFakeTimers()
    try {
      const external = new AbortController()
      mockFetch((_url?: string, init?: RequestInit) =>
        new Promise<Response>((_, reject) => {
          init?.signal?.addEventListener('abort', () => {
            setTimeout(() => {
              const error = new Error('aborted')
              error.name = 'AbortError'
              reject(error)
            }, 20)
          })
        }),
      )
      const pending = api.dashboard(auth, false, { timeoutMs: 10, signal: external.signal }).catch(
        (e: unknown) => e,
      )
      external.abort()
      await vi.advanceTimersByTimeAsync(40)
      const err = (await pending) as ApiError
      expect(err.code).toBe('aborted')
    } finally {
      vi.useRealTimers()
    }
  })
})
