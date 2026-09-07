/** Runtime checks for the additive history contracts. Amounts stay strings. */
import type { ActivityFeedPage, TradeDetailDTO, TradeEventsPage, TradeListPage, RiskPreviewDTO } from './types'
const record = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v)
const text = (v: unknown): v is string => typeof v === 'string'
const nullableText = (v: unknown) => v === null || text(v)
const money = (v: unknown) => text(v) && /^-?\d+(\.\d+)?$/.test(v)
const nullableMoney = (v: unknown) => v === null || money(v)
const integer = (v: unknown) => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0
const list = (v: unknown, valid: (x: unknown) => boolean) => Array.isArray(v) && v.every(valid)
function component(v: unknown): boolean {
  return record(v) && money(v.known_amount) && ['complete', 'partial', 'unavailable'].includes(String(v.state)) && list(v.issues, text)
}
function result(v: unknown): boolean {
  return v === null || (record(v) && money(v.gross) && nullableMoney(v.net) && component(v.commission) && component(v.funding) && component(v.builder) && list(v.issues, text) && text(v.result_state) && nullableText(v.as_of) && integer(v.revision))
}
function trade(v: unknown): boolean {
  return record(v) && text(v.trade_ref) && text(v.symbol) && nullableText(v.quantity_asset) && (v.side === null || v.side === 'LONG' || v.side === 'SHORT') && ['open', 'closed'].includes(String(v.state)) && ['copy', 'external', 'mixed', 'unknown'].includes(String(v.source)) && nullableText(v.opened_at) && nullableText(v.closed_at) && nullableMoney(v.executed_qty) && money(v.realized_pnl_gross_usdt) && nullableMoney(v.net_pnl_usdt) && ['complete', 'partial_history'].includes(String(v.quality)) && text(v.result_state) && result(v.money) && nullableMoney(v.average_entry) && nullableMoney(v.average_exit)
}
function event(v: unknown): boolean {
  return record(v) && text(v.event_ref) && text(v.text) && text(v.kind) && nullableText(v.trade_ref) && text(v.occurred_at) && record(v.facts) && Object.values(v.facts).every(x => x === null || text(x) || typeof x === 'boolean' || (typeof x === 'number' && Number.isFinite(x)))
}
function fill(v: unknown): boolean {
  return record(v) && ['trade_id', 'order_id', 'side', 'executed_at', 'role', 'position_side'].every(k => text(v[k])) && ['quantity', 'price', 'realized_pnl', 'allocated_quantity', 'allocated_realized_pnl'].every(k => money(v[k]))
}
export const validTrades = (v: unknown): v is TradeListPage => record(v) && list(v.items, trade) && nullableText(v.next_cursor) && nullableText(v.as_of) && nullableText(v.coverage_from) && typeof v.coverage_complete === 'boolean'
export const validTrade = (v: unknown): v is TradeDetailDTO => record(v) && trade(v.trade) && money(v.entry_qty) && money(v.exit_qty) && nullableMoney(v.remaining_qty) && nullableMoney(v.average_entry) && nullableMoney(v.average_exit) && (v.duration_ms === null || integer(v.duration_ms)) && integer(v.revision) && text(v.position_side) && text(v.result_state) && result(v.money)
export const validTradeEvents = (v: unknown): v is TradeEventsPage => record(v) && list(v.events, event) && list(v.fills, fill) && ['next_cursor', 'fills_cursor', 'events_cursor', 'next_fills_cursor', 'next_events_cursor'].every(k => nullableText(v[k]))
export const validFeed = (v: unknown): v is ActivityFeedPage => record(v) && list(v.items, event) && nullableText(v.next_cursor) && integer(v.high_water) && integer(v.latest_seq) && list(v.pending, x => record(x) && nullableText(x.symbol) && text(x.operation) && text(x.state) && integer(x.age_seconds))
export const validRiskPreview = (v: unknown): v is RiskPreviewDTO => record(v) && nullableMoney(v.available_usdt) && nullableText(v.snapshot_captured_at) && ['fresh', 'stale', 'unavailable'].includes(String(v.freshness)) && list(v.steps, x => record(x) && money(x.risk_percent) && money(x.budget_usdt))
