import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, type AuthContext } from './api'

const auth: AuthContext = { initData: 'signed-data', devUserId: 42 }

function mockFetch(impl: () => Promise<Response>) {
  const fetchMock = vi.fn(impl)
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('api client', () => {
  it('sends initData and dev headers and returns parsed payload', async () => {
    const fetchMock = mockFetch(async () => Response.json({ ok: 1 }))
    const result = await api.session(auth)
    expect(result).toEqual({ ok: 1 })
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    const headers = init.headers as Record<string, string>
    expect(headers['X-Telegram-Init-Data']).toBe('signed-data')
    expect(headers['X-Dev-User-Id']).toBe('42')
    expect(headers['Content-Type']).toBe('application/json')
  })

  it('omits dev header without devUserId', async () => {
    const fetchMock = mockFetch(async () => Response.json({}))
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

  it('maps transport failures to network_error', async () => {
    mockFetch(async () => {
      throw new TypeError('offline')
    })
    const err = (await api.pause(auth).catch((e: unknown) => e)) as ApiError
    expect(err.code).toBe('network_error')
    expect(err.status).toBe(0)
  })

  it('sends the confirmation object on resume', async () => {
    const fetchMock = mockFetch(async () => Response.json({ copy_state: 'active' }))
    await api.resume(auth, { token: 'tok', action: 'resume', account_id: 'tg-1' })
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({
      confirmation: { token: 'tok', action: 'resume', account_id: 'tg-1' },
    })
  })
})
