/** Typed API client: every call carries the signed Telegram initData header. */

import { validTrades, validTrade, validTradeEvents, validFeed, validRiskPreview } from './historyValidation'
import type {
  TradePeriod,
  AccountBuilderDTO,
  ActivityFilter,
  ActivityPageDTO,
  BuilderOnboardedDTO,
  BuilderPrepareDTO,
  BuilderReauthorizedDTO,
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

type Validator<T> = (value: unknown) => value is T

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function hasString(value: Record<string, unknown>, key: string): boolean {
  return typeof value[key] === 'string'
}

function hasNumber(value: Record<string, unknown>, key: string): boolean {
  return typeof value[key] === 'number' && Number.isFinite(value[key])
}

function hasBoolean(value: Record<string, unknown>, key: string): boolean {
  return typeof value[key] === 'boolean'
}

function oneOf(value: unknown, allowed: readonly string[]): boolean {
  return typeof value === 'string' && allowed.includes(value)
}

const copyStates = ['empty', 'active', 'paused', 'degraded', 'blocked'] as const
const systemStates = ['ready', 'stale', 'degraded', 'blocked', 'unknown'] as const
const accessStates = ['approved', 'pending', 'rejected', 'unknown'] as const
const connectionStates = ['empty', 'onboarding', 'recovery_required', 'connected'] as const
const onboardingStates = ['PREPARED', 'APPROVAL_SENT', 'APPROVED', 'COMMITTED'] as const
const positionSides = ['LONG', 'SHORT'] as const
const protectionStates = ['protected', 'missing_stop', 'partial_stop', 'duplicate_stop', 'unconfirmed'] as const
const activityCategories = ['trade', 'system'] as const
const activityStatuses = ['completed', 'pending', 'failed'] as const
const statusStates = ['ready', 'stale', 'degraded', 'blocked'] as const
const asterStates = ['ok', 'stale', 'blocked', 'unknown'] as const
const permissionsStates = ['valid', 'stale', 'blocked', 'unavailable'] as const
const statusReasons = ['none', 'account_not_connected', 'account_disconnected', 'system_not_ready', 'system_stale', 'system_degraded', 'aster_unavailable', 'snapshot_missing', 'snapshot_stale', 'permission_unavailable', 'permission_stale', 'permission_invalid', 'copy_blocked', 'copy_degraded'] as const
const accountBuilderStates = ['verified', 'expiring', 'stale', 'blocked', 'unavailable', 'not_applicable'] as const
const accountBuilderReasons = ['none', 'direct_account', 'account_disconnected', 'agent_invalid', 'agent_expiring', 'builder_not_confirmed', 'builder_mismatch', 'fee_mismatch', 'transient_unavailable', 'snapshot_missing', 'identity_changed'] as const

function isNumberLike(value: unknown): boolean {
  return typeof value === 'string' || (typeof value === 'number' && Number.isFinite(value))
}

function validPosition(value: unknown): boolean {
  if (!isRecord(value)) return false
  return hasString(value, 'symbol') && oneOf(value.side, positionSides) && isNumberLike(value.qty) &&
    isNumberLike(value.entry_price) && isNumberLike(value.mark_price) && isNumberLike(value.unrealized_profit) &&
    hasNumber(value, 'leverage') && oneOf(value.protection_state, protectionStates)
}

function validActivityItem(value: unknown): boolean {
  if (!isRecord(value)) return false
  return oneOf(value.category, activityCategories) && hasString(value, 'title') && oneOf(value.status, activityStatuses)
}

const validators = {
  session: ((value: unknown): value is MiniAppSessionDTO => {
    if (!isRecord(value)) return false
    return hasNumber(value, 'user_id') && oneOf(value.access_state, accessStates) && hasString(value, 'environment') && hasBoolean(value, 'account_connected') &&
      (value.connection_state === undefined || oneOf(value.connection_state, connectionStates)) &&
      (value.copy_state === undefined || oneOf(value.copy_state, copyStates)) &&
      (value.onboarding_state === undefined || value.onboarding_state === null || oneOf(value.onboarding_state, onboardingStates))
  }) satisfies Validator<MiniAppSessionDTO>,
  dashboard: ((value: unknown): value is DashboardDTO => {
    if (!isRecord(value)) return false
    return oneOf(value.copy_state, copyStates) && oneOf(value.system_state, systemStates) && hasBoolean(value, 'notification_enabled') && hasBoolean(value, 'is_admin')
  }) satisfies Validator<DashboardDTO>,
  positions: ((value: unknown): value is PositionPageDTO => {
    if (!isRecord(value)) return false
    return Array.isArray(value.positions) && value.positions.every(validPosition) && hasNumber(value, 'count') && oneOf(value.system_state, systemStates) && hasBoolean(value, 'is_stale')
  }) satisfies Validator<PositionPageDTO>,
  activity: ((value: unknown): value is ActivityPageDTO => {
    if (!isRecord(value)) return false
    return Array.isArray(value.items) && value.items.every(validActivityItem) && hasNumber(value, 'page') && hasBoolean(value, 'has_previous') && hasBoolean(value, 'has_next') && oneOf(value.filter, ['all', 'trade', 'system'])
  }) satisfies Validator<ActivityPageDTO>,
  notifications: ((value: unknown): value is NotificationSettingsDTO => {
    if (!isRecord(value)) return false
    return hasBoolean(value, 'enabled') && hasNumber(value, 'pending') && hasNumber(value, 'failed')
  }) satisfies Validator<NotificationSettingsDTO>,
  risk: ((value: unknown): value is RiskDTO => {
    if (!isRecord(value)) return false
    return hasString(value, 'risk_percent') && hasString(value, 'min') && hasString(value, 'max') && hasString(value, 'step') && Array.isArray(value.presets) && value.presets.every((item) => typeof item === 'string') && hasString(value, 'risk_base')
  }) satisfies Validator<RiskDTO>,
  status: ((value: unknown): value is StatusDTO => {
    if (!isRecord(value)) return false
    return oneOf(value.state, statusStates) && oneOf(value.system_state, systemStates) && oneOf(value.copy_state, copyStates) && oneOf(value.aster_state, asterStates) && oneOf(value.permissions_state, permissionsStates) && isRecord(value.permissions) && oneOf(value.reason, statusReasons)
  }) satisfies Validator<StatusDTO>,
  confirmation: ((value: unknown): value is ConfirmationDTO => isRecord(value) && hasString(value, 'token') && hasString(value, 'action')) satisfies Validator<ConfirmationDTO>,
  builderPrepare: ((value: unknown): value is BuilderPrepareDTO => isRecord(value) && ('pending_id' in value) && (value.pending_id === null || typeof value.pending_id === 'string')) satisfies Validator<BuilderPrepareDTO>,
  builderOnboarded: ((value: unknown): value is BuilderOnboardedDTO => {
    if (!isRecord(value)) return false
    return hasString(value, 'account_id') && hasString(value, 'account_masked') && hasString(value, 'signer_masked') && hasString(value, 'builder_name') && hasString(value, 'max_fee_rate') && hasString(value, 'position_mode') && (typeof value.available_usdt === 'string' || typeof value.available_usdt === 'number') && hasNumber(value, 'agent_expires_at_ms') && hasBoolean(value, 'paused')
  }) satisfies Validator<BuilderOnboardedDTO>,
  accountBuilder: ((value: unknown): value is AccountBuilderDTO => isRecord(value) && oneOf(value.identity_source, ['direct', 'builder']) && oneOf(value.state, accountBuilderStates) && oneOf(value.reason, accountBuilderReasons) && isRecord(value.permission)) satisfies Validator<AccountBuilderDTO>,
  builderReauthorized: ((value: unknown): value is BuilderReauthorizedDTO => {
    if (!isRecord(value)) return false
    return hasString(value, 'account_id') && hasString(value, 'account_masked') && hasString(value, 'signer_masked') && hasString(value, 'builder_name') && hasString(value, 'max_fee_rate') && hasNumber(value, 'agent_expires_at_ms') && hasBoolean(value, 'paused')
  }) satisfies Validator<BuilderReauthorizedDTO>,
  ok: ((value: unknown): value is { ok: boolean } => isRecord(value) && hasBoolean(value, 'ok')) satisfies Validator<{ ok: boolean }>,
}

function authHeaders(auth: AuthContext): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (auth.initData) headers['X-Telegram-Init-Data'] = auth.initData
  if (auth.devUserId) headers['X-Dev-User-Id'] = String(auth.devUserId)
  return headers
}

const DEFAULT_TIMEOUT_MS = 15_000

type AbortKind = 'timeout' | 'external'

function abortedError(): Error {
  const error = new Error('aborted')
  error.name = 'AbortError'
  return error
}

/** Keep the request deadline alive through body consumption as well as headers.
 * Browsers normally reject `response.json()` when the fetch signal is aborted,
 * but racing explicitly also makes the contract deterministic for WebViews and
 * test doubles whose Response body is not wired back to the fetch signal. */
async function readJson(response: Response, signal: AbortSignal): Promise<unknown> {
  if (signal.aborted) throw abortedError()
  return await new Promise<unknown>((resolve, reject) => {
    const onAbort = () => reject(abortedError())
    signal.addEventListener('abort', onAbort, { once: true })
    void response.json().then(resolve, reject).finally(() => {
      signal.removeEventListener('abort', onAbort)
    })
  })
}

export async function post<T>(
  path: string,
  body: unknown,
  auth: AuthContext,
  options?: { timeoutMs?: number; signal?: AbortSignal },
  validate?: Validator<T>,
): Promise<T> {
  const timeoutMs = options?.timeoutMs ?? DEFAULT_TIMEOUT_MS
  const controller = new AbortController()
  let abortKind: AbortKind | null = null
  const timeoutId = setTimeout(() => {
    if (controller.signal.aborted || abortKind !== null) return
    abortKind = 'timeout'
    controller.abort('timeout')
  }, timeoutMs)

  const externalSignal = options?.signal
  const onExternalAbort = () => {
    if (controller.signal.aborted) return
    abortKind = 'external'
    controller.abort('aborted')
  }

  if (externalSignal) {
    if (externalSignal.aborted) {
      clearTimeout(timeoutId)
      throw new ApiError(0, 'aborted', 'Запрос отменён.')
    }
    externalSignal.addEventListener('abort', onExternalAbort, { once: true })
  }

  try {
    const response = await fetch(path, {
      method: 'POST',
      headers: authHeaders(auth),
      body: JSON.stringify(body ?? {}),
      signal: controller.signal,
    })
    let data: unknown = null
    let validJson = true
    try {
      data = await readJson(response, controller.signal)
    } catch (error) {
      if (controller.signal.aborted) throw error
      validJson = false
    }
    if (!response.ok) {
      const record = (validJson ? (data ?? {}) : {}) as { error?: string; message?: string }
      throw new ApiError(
        response.status,
        record.error ?? 'unclassified_error',
        record.message ?? 'Сервер вернул ошибку без описания.',
      )
    }
    const validPayload =
      validJson &&
      data !== null &&
      typeof data === 'object' &&
      !Array.isArray(data) &&
      Object.keys(data as Record<string, unknown>).length > 0 &&
      (validate?.(data) ?? true)
    if (!validPayload) {
      throw new ApiError(response.status, 'invalid_response', 'Сервер вернул некорректный ответ.')
    }
    return data as T
  } catch (error) {
    if (error instanceof ApiError) throw error
    if (controller.signal.aborted) {
      if (abortKind === 'external') {
        throw new ApiError(0, 'aborted', 'Запрос отменён.')
      }
      throw new ApiError(0, 'timeout_error', 'Время ожидания ответа сервера истекло. Попробуйте снова.')
    }
    throw new ApiError(0, 'network_error', 'Нет связи с сервером. Проверьте интернет.')
  } finally {
    clearTimeout(timeoutId)
    externalSignal?.removeEventListener('abort', onExternalAbort)
  }
}
export const api = {
  session: (auth: AuthContext, options?: { timeoutMs?: number; signal?: AbortSignal }) =>
    post<MiniAppSessionDTO>('/miniapp/api/session', {}, auth, options, validators.session),

  dashboard: (
    auth: AuthContext,
    refresh = false,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ) => post<DashboardDTO>('/miniapp/api/dashboard', { refresh }, auth, options, validators.dashboard),

  positions: (
    auth: AuthContext,
    refresh = false,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ) => post<PositionPageDTO>('/miniapp/api/positions', { refresh }, auth, options, validators.positions),

  activity: (
    auth: AuthContext,
    filter: ActivityFilter = 'all',
    page = 0,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ) => post<ActivityPageDTO>('/miniapp/api/activity', { filter, page }, auth, options, validators.activity),

  trades: (auth: AuthContext, query: { period?: TradePeriod; cursor?: string | null; limit?: number; symbol?: string } = {}, options?: { signal?: AbortSignal }) =>
    post('/miniapp/api/trades', query, auth, options, validTrades),
  trade: (auth: AuthContext, tradeRef: string, options?: { signal?: AbortSignal }) =>
    post('/miniapp/api/trade', { trade_ref: tradeRef }, auth, options, validTrade),
  tradeEvents: (auth: AuthContext, tradeRef: string, query: { fills_cursor?: string | null; events_cursor?: string | null; fills_limit?: number; events_limit?: number } = {}, options?: { signal?: AbortSignal }) =>
    post('/miniapp/api/trade/events', { trade_ref: tradeRef, ...query }, auth, options, validTradeEvents),
  activityFeed: (auth: AuthContext, filter: ActivityFilter = 'all', cursor: string | null = null, options?: { signal?: AbortSignal }) =>
    post('/miniapp/api/activity/feed', { filter, cursor, limit: 20 }, auth, options, validFeed),
  riskPreview: (auth: AuthContext, options?: { signal?: AbortSignal }) =>
    post('/miniapp/api/risk/preview', {}, auth, options, validRiskPreview),

  notifications: (
    auth: AuthContext,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ) => post<NotificationSettingsDTO>('/miniapp/api/notifications', {}, auth, options, validators.notifications),

  setNotifications: (
    auth: AuthContext,
    enabled: boolean,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ) => post<NotificationSettingsDTO>('/miniapp/api/notifications/enabled', { enabled }, auth, options, validators.notifications),

  risk: (auth: AuthContext, options?: { timeoutMs?: number; signal?: AbortSignal }) =>
    post<RiskDTO>('/miniapp/api/risk', {}, auth, options, validators.risk),

  status: (
    auth: AuthContext,
    refresh = false,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ) => post<StatusDTO>('/miniapp/api/status', { refresh }, auth, options, validators.status),

  /** riskPercent travels as a fraction string, e.g. "0.02" for 2,0%. */
  setRiskPercent: (
    auth: AuthContext,
    riskPercent: string,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ) => post<RiskDTO>('/miniapp/api/risk/percent', { risk_percent: riskPercent }, auth, options, validators.risk),
  pause: (auth: AuthContext) => post<DashboardDTO>('/miniapp/api/pause', {}, auth, undefined, validators.dashboard),

  resume: (auth: AuthContext, confirmation?: ConfirmationDTO) =>
    post<DashboardDTO>('/miniapp/api/resume', { confirmation }, auth, undefined, validators.dashboard),

  beginConfirmation: (auth: AuthContext, action: string) =>
    post<ConfirmationDTO>('/miniapp/api/confirmation/begin', { action }, auth, undefined, validators.confirmation),

  confirmConfirmation: (auth: AuthContext, confirmation: ConfirmationDTO) =>
    post<DashboardDTO>('/miniapp/api/confirmation/confirm', { confirmation }, auth, undefined, validators.dashboard),

  builderPrepare: (auth: AuthContext, userAddress: string) =>
    post<BuilderPrepareDTO>('/miniapp/api/builder/prepare', { user_address: userAddress }, auth, undefined, validators.builderPrepare),

  builderSubmit: (auth: AuthContext, pendingId: string, signature: string) =>
    post<BuilderOnboardedDTO>(
      '/miniapp/api/builder/submit',
      { pending_id: pendingId, signature },
      auth,
      undefined,
      validators.builderOnboarded,
    ),
  builderResumeAttempt: (
    auth: AuthContext,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ) => post<BuilderOnboardedDTO>('/miniapp/api/builder/resume-attempt', {}, auth, options, validators.builderOnboarded),
  builderRecover: (
    auth: AuthContext,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ) => post<MiniAppSessionDTO>('/miniapp/api/builder/recover', {}, auth, options, validators.session),

  accountBuilder: (
    auth: AuthContext,
    refresh = false,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ) => post<AccountBuilderDTO>('/miniapp/api/account-builder', { refresh }, auth, options, validators.accountBuilder),

  reauthorizePrepare: (auth: AuthContext) =>
    post<BuilderPrepareDTO>('/miniapp/api/builder/reauthorize-prepare', {}, auth, undefined, validators.builderPrepare),

  reauthorizeSubmit: (auth: AuthContext, pendingId: string, signature: string) =>
    post<BuilderReauthorizedDTO>(
      '/miniapp/api/builder/reauthorize-submit',
      { pending_id: pendingId, signature },
      auth,
      undefined,
      validators.builderReauthorized,
    ),

  /** Fire-and-forget diagnostics: lands in the operator's server log. */
  clientLog: (
    auth: AuthContext,
    payload: { level: string; event: string; code?: string | null; detail?: string | null },
  ) => post<{ ok: boolean }>('/miniapp/api/client-log', payload, auth, undefined, validators.ok).catch(() => ({ ok: false })),
}
