/** Typed API client: every call carries the signed Telegram initData header. */

import type {
  BuilderOnboardedDTO,
  BuilderPrepareDTO,
  ConfirmationDTO,
  DashboardDTO,
  MiniAppSessionDTO,
} from './types'

export class ApiError extends Error {
  readonly status: number
  readonly code: string

  constructor(status: number, code: string, message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

export interface AuthContext {
  /** Signed Telegram initData; empty outside Telegram (dev mode only). */
  initData: string
  /** Declared dev user id; used only when the server runs CT_MINIAPP_DEV_UNSIGNED. */
  devUserId?: number
}

function authHeaders(auth: AuthContext): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (auth.initData) headers['X-Telegram-Init-Data'] = auth.initData
  if (auth.devUserId) headers['X-Dev-User-Id'] = String(auth.devUserId)
  return headers
}

export async function post<T>(path: string, body: unknown, auth: AuthContext): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      method: 'POST',
      headers: authHeaders(auth),
      body: JSON.stringify(body ?? {}),
    })
  } catch {
    throw new ApiError(0, 'network_error', 'Нет связи с сервером. Проверьте интернет.')
  }
  const data: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const record = (data ?? {}) as { error?: string; message?: string }
    throw new ApiError(
      response.status,
      record.error ?? 'unclassified_error',
      record.message ?? 'Сервер вернул ошибку без описания.',
    )
  }
  return data as T
}

export const api = {
  session: (auth: AuthContext) => post<MiniAppSessionDTO>('/miniapp/api/session', {}, auth),

  dashboard: (auth: AuthContext) => post<DashboardDTO>('/miniapp/api/dashboard', {}, auth),

  pause: (auth: AuthContext) => post<DashboardDTO>('/miniapp/api/pause', {}, auth),

  resume: (auth: AuthContext, confirmation?: ConfirmationDTO) =>
    post<DashboardDTO>('/miniapp/api/resume', { confirmation }, auth),

  beginConfirmation: (auth: AuthContext, action: string) =>
    post<ConfirmationDTO>('/miniapp/api/confirmation/begin', { action }, auth),

  builderPrepare: (auth: AuthContext, userAddress: string) =>
    post<BuilderPrepareDTO>('/miniapp/api/builder/prepare', { user_address: userAddress }, auth),

  builderSubmit: (auth: AuthContext, pendingId: string, signature: string) =>
    post<BuilderOnboardedDTO>(
      '/miniapp/api/builder/submit',
      { pending_id: pendingId, signature },
      auth,
    ),

  /** Fire-and-forget diagnostics: lands in the operator's server log. */
  clientLog: (
    auth: AuthContext,
    payload: { level: string; event: string; code?: string | null; detail?: string | null },
  ) => post<{ ok: boolean }>('/miniapp/api/client-log', payload, auth).catch(() => ({ ok: false })),
}
