/** Typed API client: every call carries the signed Telegram initData header. */

import type {
  ActivityFilter,
  ActivityPageDTO,
  BuilderOnboardedDTO,
  BuilderPrepareDTO,
  ConfirmationDTO,
  DashboardDTO,
  MiniAppSessionDTO,
  NotificationSettingsDTO,
  PositionPageDTO,
  RiskDTO,
  StatusDTO,
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

const DEFAULT_TIMEOUT_MS = 15_000

export async function post<T>(
  path: string,
  body: unknown,
  auth: AuthContext,
  options?: { timeoutMs?: number; signal?: AbortSignal },
): Promise<T> {
  const timeoutMs = options?.timeoutMs ?? DEFAULT_TIMEOUT_MS
  const controller = new AbortController()
  const timeoutId = setTimeout(() => controller.abort('timeout'), timeoutMs)

  if (options?.signal) {
    if (options.signal.aborted) {
      clearTimeout(timeoutId)
      throw new ApiError(0, 'aborted', 'Запрос отменён.')
    }
    options.signal.addEventListener('abort', () => controller.abort('aborted'), { once: true })
  }

  let response: Response
  try {
    response = await fetch(path, {
      method: 'POST',
      headers: authHeaders(auth),
      body: JSON.stringify(body ?? {}),
      signal: controller.signal,
    })
  } catch {
    if (controller.signal.aborted) {
      if (options?.signal?.aborted) {
        throw new ApiError(0, 'aborted', 'Запрос отменён.')
      }
      throw new ApiError(0, 'timeout_error', 'Время ожидания ответа сервера истекло. Попробуйте снова.')
    }
    throw new ApiError(0, 'network_error', 'Нет связи с сервером. Проверьте интернет.')
  } finally {
    clearTimeout(timeoutId)
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
  session: (auth: AuthContext, options?: { timeoutMs?: number; signal?: AbortSignal }) =>
    post<MiniAppSessionDTO>('/miniapp/api/session', {}, auth, options),

  dashboard: (
    auth: AuthContext,
    refresh = false,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ) => post<DashboardDTO>('/miniapp/api/dashboard', { refresh }, auth, options),

  positions: (
    auth: AuthContext,
    refresh = false,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ) => post<PositionPageDTO>('/miniapp/api/positions', { refresh }, auth, options),

  activity: (
    auth: AuthContext,
    filter: ActivityFilter = 'all',
    page = 0,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ) => post<ActivityPageDTO>('/miniapp/api/activity', { filter, page }, auth, options),

  notifications: (
    auth: AuthContext,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ) => post<NotificationSettingsDTO>('/miniapp/api/notifications', {}, auth, options),

  setNotifications: (
    auth: AuthContext,
    enabled: boolean,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ) => post<NotificationSettingsDTO>('/miniapp/api/notifications/enabled', { enabled }, auth, options),

  risk: (auth: AuthContext, options?: { timeoutMs?: number; signal?: AbortSignal }) =>
    post<RiskDTO>('/miniapp/api/risk', {}, auth, options),

  status: (
    auth: AuthContext,
    refresh = false,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ) => post<StatusDTO>('/miniapp/api/status', { refresh }, auth, options),

  /** riskPercent travels as a fraction string, e.g. "0.02" for 2,0%. */
  setRiskPercent: (
    auth: AuthContext,
    riskPercent: string,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ) => post<RiskDTO>('/miniapp/api/risk/percent', { risk_percent: riskPercent }, auth, options),
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
  builderResumeAttempt: (
    auth: AuthContext,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ) => post<BuilderOnboardedDTO>('/miniapp/api/builder/resume-attempt', {}, auth, options),
  builderRecover: (
    auth: AuthContext,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ) => post<MiniAppSessionDTO>('/miniapp/api/builder/recover', {}, auth, options),

  /** Fire-and-forget diagnostics: lands in the operator's server log. */
  clientLog: (
    auth: AuthContext,
    payload: { level: string; event: string; code?: string | null; detail?: string | null },
  ) => post<{ ok: boolean }>('/miniapp/api/client-log', payload, auth).catch(() => ({ ok: false })),
}
