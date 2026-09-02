/** Mirrors of the server-side transport-neutral DTOs (application/models.py). */

export type CopyState = 'empty' | 'active' | 'paused' | 'degraded' | 'blocked'

export type SystemState = 'ready' | 'stale' | 'degraded' | 'blocked' | 'unknown'

export type AccessState = 'approved' | 'pending' | 'rejected' | 'unknown'

export type ConnectionState = 'empty' | 'onboarding' | 'recovery_required' | 'connected'

export type OnboardingState = 'PREPARED' | 'APPROVAL_SENT' | 'APPROVED' | 'COMMITTED'

export interface MiniAppSessionDTO {
  user_id: number
  access_state: AccessState
  environment: string
  connection_state?: ConnectionState
  account_connected: boolean
  account_masked?: string | null
  copy_state?: CopyState
  is_admin?: boolean
  recovery_reason?: string | null
  onboarding_state?: OnboardingState | null
}

export interface BuilderPrepareDTO {
  pending_id: string | null
  user_masked?: string | null
  agent_masked?: string | null
  builder_masked?: string | null
  builder_name?: string | null
  max_fee_rate?: string | null
  signature_plan?: string
  can_perp_trade?: boolean
  can_withdraw?: boolean
  agent_expires_at_ms?: number | null
  approval_expires_at_ms?: number | null
  already_connected?: boolean
  eip712?: Record<string, unknown>
}
export interface BuilderOnboardedDTO {
  account_id: string
  account_masked: string
  signer_masked: string
  builder_name: string
  max_fee_rate: string
  position_mode: string
  available_usdt: string | number
  agent_expires_at_ms: number
  paused: boolean
  message?: string | null
}

export interface DashboardDTO {
  copy_state: CopyState
  system_state: SystemState
  account_id?: string | null
  account_masked?: string | null
  signer_masked?: string | null
  risk_percent?: string | null
  max_leverage?: number | null
  open_positions?: number | null
  updated_at?: string | null
  snapshot_captured_at?: string | null
  message?: string | null
  notification_enabled: boolean
  is_admin: boolean
}

export interface ConfirmationDTO {
  token: string
  action: string
  account_id?: string | null
  expires_at?: string | null
}

export type PositionSide = 'LONG' | 'SHORT'

export type PositionProtectionState =
  | 'protected'
  | 'missing_stop'
  | 'partial_stop'
  | 'duplicate_stop'
  | 'unconfirmed'

export interface PositionSummaryDTO {
  symbol: string
  side: PositionSide
  qty: string | number
  entry_price: string | number
  mark_price: string | number
  unrealized_profit: string | number
  leverage: number
  protection_state: PositionProtectionState
  stop_price?: string | number | null
  take_profit_price?: string | number | null
  risk_to_stop_usdt?: string | number | null
}

export interface PositionPageDTO {
  positions: PositionSummaryDTO[]
  count: number
  system_state: SystemState
  snapshot_captured_at?: string | null
  is_stale: boolean
  message?: string | null
}

export type ActivityCategory = 'trade' | 'system'

export type ActivityStatus = 'completed' | 'pending' | 'failed'

export type ActivityFilter = 'all' | 'trade' | 'system'

export interface ActivityItemDTO {
  category: ActivityCategory
  title: string
  status: ActivityStatus
  symbol?: string | null
  occurred_at?: string | null
  pending_age?: number | null
  remediation?: string | null
}

export interface ActivityPageDTO {
  items: ActivityItemDTO[]
  page: number
  has_previous: boolean
  has_next: boolean
  filter: ActivityFilter
}

export interface NotificationSettingsDTO {
  enabled: boolean
  last_delivery_at?: string | null
  pending: number
  failed: number
}
