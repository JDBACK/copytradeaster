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
  can_perp_trade?: boolean | null
  can_withdraw?: boolean | null
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
  builder_verified?: boolean
  overview?: AccountOverview | null
}

export interface ConfirmationDTO {
  token: string
  action: string
  account_id?: string | null
  expires_at?: string | null
  resume_context?: ResumeContextDTO | null
}

export type PositionSide = 'LONG' | 'SHORT'

export type PositionProtectionState =
  | 'protected'
  | 'missing_stop'
  | 'partial_stop'
  | 'duplicate_stop'
  | 'unconfirmed'

export interface PositionSummaryDTO {
  quantity_asset?: string | null
  trade_ref?: string | null
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

export interface RiskDTO {
  risk_percent: string
  min: string
  max: string
  step: string
  presets: string[]
  risk_base: string
}

export type StatusState = 'ready' | 'stale' | 'degraded' | 'blocked'

export type AsterState = 'ok' | 'stale' | 'blocked' | 'unknown'

export type PermissionsState = 'valid' | 'stale' | 'blocked' | 'unavailable'

export type StatusReason =
  | 'none'
  | 'account_not_connected'
  | 'account_disconnected'
  | 'system_not_ready'
  | 'system_stale'
  | 'system_degraded'
  | 'aster_unavailable'
  | 'snapshot_missing'
  | 'snapshot_stale'
  | 'permission_unavailable'
  | 'permission_stale'
  | 'permission_invalid'
  | 'copy_blocked'
  | 'copy_degraded'

/** Executable remediation set (P2.5): only refresh / open_bot / absent. */
export type StatusRemediation = 'refresh' | 'open_bot'

export interface StatusPermissionsDTO {
  can_read?: boolean | null
  can_perp_trade?: boolean | null
  can_withdraw?: boolean | null
  whitelist_ok?: boolean | null
  checked_at?: string | null
  warning?: string | null
}

/** Mirror of the server-side P2.5 StatusDTO (application/models.py). */
export interface StatusDTO {
  state: StatusState
  system_state: SystemState
  copy_state: CopyState
  aster_state: AsterState
  permissions_state: PermissionsState
  account_masked?: string | null
  signer_masked?: string | null
  positions_count?: number | null
  permissions: StatusPermissionsDTO
  snapshot_captured_at?: string | null
  last_check_at?: string | null
  age_seconds?: number | null
  reason: StatusReason
  remediation?: StatusRemediation | null
  builder_verified?: boolean
}

/** Mirror of the server-side P2.6 AccountBuilderDTO (application/models.py). */
export type AccountBuilderState =
  | 'verified'
  | 'expiring'
  | 'stale'
  | 'blocked'
  | 'unavailable'
  | 'not_applicable'

export type AccountBuilderReason =
  | 'none'
  | 'direct_account'
  | 'account_disconnected'
  | 'agent_invalid'
  | 'agent_expiring'
  | 'builder_not_confirmed'
  | 'builder_mismatch'
  | 'fee_mismatch'
  | 'transient_unavailable'
  | 'snapshot_missing'
  | 'identity_changed'

export type AccountBuilderRemediation = 'refresh' | 'reauthorize'

export interface AccountBuilderDTO {
  identity_source: 'direct' | 'builder'
  state: AccountBuilderState
  reason: AccountBuilderReason
  remediation?: AccountBuilderRemediation | null
  account_masked?: string | null
  signer_masked?: string | null
  builder_address_masked?: string | null
  builder_name?: string | null
  max_fee_rate?: string | null
  permission: StatusPermissionsDTO
  agent_expires_at_ms?: number | null
  verified_at?: string | null
  last_check_at?: string | null
  age_seconds?: number | null
}

export interface BuilderReauthorizedDTO {
  account_id: string
  account_masked: string
  signer_masked: string
  builder_name: string
  max_fee_rate: string
  agent_expires_at_ms: number
  paused: boolean
  message?: string | null
}


export type SnapshotFreshness = 'fresh' | 'stale' | 'unavailable'
export interface AccountOverview {
  as_of: string | null
  freshness: SnapshotFreshness
  open_count: number | null
  unrealized_pnl_usdt: string | null
  available_usdt: string | null
  protected_count: number | null
  risk_coverage: 'complete' | 'partial' | 'unavailable'
}
export interface ResumeContextDTO {
  master_in_position: boolean | null
  snapshot_freshness: SnapshotFreshness
  permission_ok: boolean | null
}
export interface RiskPreviewDTO {
  available_usdt: string | null
  steps: { risk_percent: string; budget_usdt: string }[]
  snapshot_captured_at: string | null
  freshness: SnapshotFreshness
}
export interface MoneyComponent {
  known_amount: string
  state: 'complete' | 'partial' | 'unavailable'
  issues: string[]
}
export interface EpisodeMoneyView {
  gross: string
  net: string | null
  commission: MoneyComponent
  funding: MoneyComponent
  builder: MoneyComponent
  result_state: string
  issues: string[]
  as_of: string | null
  revision: number
}
export interface TradeListItem {
  trade_ref: string
  symbol: string
  quantity_asset: string | null
  side: PositionSide | null
  state: 'open' | 'closed'
  source: 'copy' | 'external' | 'mixed' | 'unknown'
  opened_at: string | null
  closed_at: string | null
  executed_qty: string | null
  realized_pnl_gross_usdt: string
  net_pnl_usdt: string | null
  quality: 'complete' | 'partial_history'
  result_state: string
  money: EpisodeMoneyView | null
  average_entry: string | null
  average_exit: string | null
}
export interface TradeListPage {
  items: TradeListItem[]
  next_cursor: string | null
  as_of: string | null
  coverage_from: string | null
  coverage_complete: boolean
}
export interface TradeDetailDTO {
  trade: TradeListItem
  entry_qty: string
  exit_qty: string
  remaining_qty: string | null
  average_entry: string | null
  average_exit: string | null
  duration_ms: number | null
  revision: number
  position_side: string
  result_state: string
  money: EpisodeMoneyView | null
}
export interface TradeEventItem {
  text: string
  event_ref: string
  kind: string
  trade_ref: string | null
  occurred_at: string
  facts: Record<string, string | number | boolean | null>
}
export interface FillFacts {
  trade_id: string
  order_id: string
  side: string
  quantity: string
  price: string
  realized_pnl: string
  executed_at: string
  allocated_quantity: string
  role: string
  allocated_realized_pnl: string
  position_side: string
}
export interface TradeEventsPage {
  fills: FillFacts[]
  events: TradeEventItem[]
  next_cursor: string | null
  fills_cursor: string | null
  events_cursor: string | null
  next_fills_cursor: string | null
  next_events_cursor: string | null
}
export interface ActivityFeedPage {
  items: TradeEventItem[]
  next_cursor: string | null
  high_water: number
  latest_seq: number
  pending: { symbol: string | null; operation: string; state: string; age_seconds: number }[]
}
export type TradePeriod = '7d' | '30d' | 'all'
