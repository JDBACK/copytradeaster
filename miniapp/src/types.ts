/** Mirrors of the server-side transport-neutral DTOs (application/models.py). */

export type CopyState = 'empty' | 'active' | 'paused' | 'degraded' | 'blocked'

export type SystemState = 'ready' | 'stale' | 'degraded' | 'blocked' | 'unknown'

export type AccessState = 'approved' | 'pending' | 'rejected' | 'unknown'

export interface MiniAppSessionDTO {
  user_id: number
  access_state: AccessState
  environment: string
  account_connected: boolean
  account_masked?: string | null
  copy_state?: CopyState
  is_admin?: boolean
}

export interface BuilderPrepareDTO {
  pending_id: string
  user_masked: string
  agent_masked: string
  builder_masked: string
  builder_name: string
  max_fee_rate: string
  signature_plan: string
  can_perp_trade: boolean
  can_withdraw: boolean
  agent_expires_at_ms: number
  approval_expires_at_ms: number
  eip712: Record<string, unknown>
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
