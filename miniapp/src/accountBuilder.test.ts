/** Pure view-model tests for the Account + Builder attestation (P2.6).
 *
 * The single shared Builder verdict is verified-only: snapshot presence,
 * builder_name or max_fee_rate never imply verification on any surface
 * (Account screen, Home, System Status). */

import { describe, expect, it } from 'vitest'
import {
  DISCONNECT_CONFIRM_BODY,
  isAccountBuilderVerified,
  isBuilderVerified,
  reasonCopy,
  stateCopy,
} from './accountBuilder'
import type { AccountBuilderDTO } from './types'

function dto(overrides: Partial<AccountBuilderDTO> = {}): AccountBuilderDTO {
  return {
    identity_source: 'builder',
    state: 'verified',
    reason: 'none',
    remediation: null,
    account_masked: '0x1111…1111',
    signer_masked: '0x2222…2222',
    builder_address_masked: '0x2576…6EF8',
    builder_name: 'copytradeaster',
    max_fee_rate: '0.0001',
    permission: { can_read: true, can_perp_trade: true, can_withdraw: false },
    agent_expires_at_ms: 2_000_000_000_000,
    verified_at: '2026-09-03T12:00:00+00:00',
    last_check_at: '2026-09-03T12:00:00+00:00',
    age_seconds: 0,
    ...overrides,
  }
}

describe('isBuilderVerified', () => {
  it('is true only for the explicit backend flag', () => {
    expect(isBuilderVerified({ builder_verified: true })).toBe(true)
    expect(isBuilderVerified({ builder_verified: false })).toBe(false)
    expect(isBuilderVerified({})).toBe(false)
  })
})

describe('isAccountBuilderVerified', () => {
  it('is true only for the verified state', () => {
    expect(isAccountBuilderVerified(dto())).toBe(true)
    for (const state of ['expiring', 'stale', 'blocked', 'unavailable', 'not_applicable'] as const) {
      expect(isAccountBuilderVerified(dto({ state }))).toBe(false)
    }
  })

  it('never verifies from snapshot fields alone', () => {
    // A full snapshot with a non-verified state is not verification.
    const stale = dto({ state: 'stale', remediation: 'refresh' })
    expect(stale.builder_name).toBe('copytradeaster')
    expect(stale.max_fee_rate).toBe('0.0001')
    expect(isAccountBuilderVerified(stale)).toBe(false)
  })
})

describe('stateCopy', () => {
  it('maps every backend state to an honest Russian label', () => {
    expect(stateCopy('verified')).toEqual({ label: 'Builder подтверждён', tone: 'success' })
    expect(stateCopy('expiring').tone).toBe('warning')
    expect(stateCopy('stale').tone).toBe('warning')
    expect(stateCopy('blocked').tone).toBe('danger')
    expect(stateCopy('unavailable').tone).toBe('muted')
    expect(stateCopy('not_applicable')).toEqual({ label: 'Прямое подключение', tone: 'muted' })
  })

  it('never renders stale as verified', () => {
    expect(stateCopy('stale').label).not.toContain('подтверждён')
    expect(stateCopy('stale').label).not.toContain('Подтверждён')
  })
})

describe('reasonCopy', () => {
  it('explains every backend reason without leaking internals', () => {
    for (const reason of [
      'none',
      'direct_account',
      'account_disconnected',
      'agent_invalid',
      'agent_expiring',
      'builder_not_confirmed',
      'builder_mismatch',
      'fee_mismatch',
      'transient_unavailable',
      'snapshot_missing',
      'identity_changed',
    ] as const) {
      const copy = reasonCopy(reason)
      expect(copy.length).toBeGreaterThan(0)
      expect(copy).not.toMatch(/0x[0-9a-fA-F]{4,}/)
    }
  })
})

describe('disconnect copy', () => {
  it('states that Aster-side authorization is NOT revoked', () => {
    expect(DISCONNECT_CONFIRM_BODY).toContain('не отзывается')
  })
})
