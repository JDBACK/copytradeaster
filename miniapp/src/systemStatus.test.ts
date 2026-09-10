/** Unit tests for the System Status view-model (P2.5): closed mappings over
 * the authoritative StatusDTO — no invented health, neutral Builder, and an
 * executable remediation set limited to refresh / open_bot / absent. */

import { describe, expect, it } from 'vitest'
import {
  asterRowValue,
  copyRowValue,
  formatAge,
  lastCheckRowValue,
  overallValue,
  permissionsRowValue,
  positionsRowValue,
  reasonCopy,
  remediationOf,
  statusTrust,
  systemRowValue,
  TRANSPORT_STALE_WARNING,
} from './systemStatus'
import type { StatusDTO } from './types'

function dto(overrides: Partial<StatusDTO> = {}): StatusDTO {
  return {
    state: 'ready',
    system_state: 'ready',
    copy_state: 'paused',
    aster_state: 'ok',
    permissions_state: 'valid',
    account_masked: '0x3c45…F975',
    signer_masked: '0x2222…2222',
    positions_count: 0,
    permissions: { can_read: true, can_perp_trade: true, can_withdraw: false },
    snapshot_captured_at: '2026-09-03T12:00:00+00:00',
    last_check_at: '2026-09-03T12:00:00+00:00',
    age_seconds: 5,
    reason: 'none',
    remediation: null,
    ...overrides,
  }
}

describe('overallValue', () => {
  it('reads healthy only from backend ready', () => {
    expect(overallValue('ready')).toEqual({ label: 'Всё работает', tone: 'success' })
    expect(overallValue('stale').tone).toBe('warning')
    expect(overallValue('degraded').tone).toBe('warning')
    expect(overallValue('blocked').tone).toBe('danger')
    expect(overallValue(undefined).tone).toBe('muted')
  })
})

describe('diagnostic row values', () => {
  it('maps every row without inventing facts', () => {
    expect(systemRowValue('ready')).toEqual({ label: 'Работает', tone: 'success' })
    expect(systemRowValue('stale')).toEqual({ label: 'Данные устарели', tone: 'warning' })
    expect(copyRowValue('active')).toEqual({ label: 'Включено', tone: 'success' })
    expect(copyRowValue('paused')).toEqual({ label: 'На паузе', tone: 'warning' })
    expect(asterRowValue('ok')).toEqual({ label: 'Подключён', tone: 'success' })
    expect(asterRowValue('unknown')).toEqual({ label: 'Неизвестно', tone: 'muted' })
    expect(permissionsRowValue('valid')).toEqual({ label: 'Подтверждены', tone: 'success' })
    expect(permissionsRowValue('blocked')).toEqual({ label: 'Отклонены', tone: 'danger' })
    expect(positionsRowValue(2)).toEqual({ label: '2', tone: 'muted' })
    expect(positionsRowValue(null)).toEqual({ label: '—', tone: 'muted' })
    expect(positionsRowValue(undefined)).toEqual({ label: '—', tone: 'muted' })
  })

  it('formats the last check from backend evidence only', () => {
    const value = lastCheckRowValue('2026-09-03T12:00:00+00:00', 45)
    expect(value.label).toContain('45 сек назад')
    expect(lastCheckRowValue(null, null)).toEqual({ label: 'Не проверялось', tone: 'muted' })
  })

  it('formats ages honestly', () => {
    expect(formatAge(0)).toBe('только что')
    expect(formatAge(45)).toBe('45 сек назад')
    expect(formatAge(120)).toBe('2 мин назад')
    expect(formatAge(null)).toBe('—')
  })
})

describe('reasonCopy', () => {
  it('returns null for fresh none and covers every typed reason', () => {
    expect(reasonCopy('none')).toBeNull()
    const reasons = [
      'account_not_connected',
      'account_disconnected',
      'system_not_ready',
      'system_stale',
      'system_degraded',
      'aster_unavailable',
      'snapshot_missing',
      'snapshot_stale',
      'permission_unavailable',
      'permission_stale',
      'permission_invalid',
      'copy_blocked',
      'copy_degraded',
    ] as const
    for (const reason of reasons) {
      const copy = reasonCopy(reason)
      expect(typeof copy).toBe('string')
      expect(copy?.length).toBeGreaterThan(0)
    }
  })
})

describe('remediationOf', () => {
  it('admits only the closed executable set', () => {
    expect(remediationOf('refresh')).toBe('refresh')
    expect(remediationOf('open_bot')).toBe('open_bot')
    expect(remediationOf(null)).toBeNull()
    expect(remediationOf(undefined)).toBeNull()
    expect(remediationOf('open_url')).toBeNull()
    expect(remediationOf('run_js')).toBeNull()
    expect(remediationOf('')).toBeNull()
  })
})

describe('statusTrust', () => {
  it('derives Кошелёк/Agent/Сверка from evidence and keeps Builder neutral', () => {
    const trust = statusTrust(dto())
    expect(trust).toEqual([
      { key: 'wallet', label: 'Кошелёк', done: true },
      { key: 'agent', label: 'Agent', done: true },
      { key: 'builder', label: 'Builder', done: false },
      { key: 'sync', label: 'Сверка', done: true },
    ])
  })

  it('never marks Builder verified and drops Сверка without ready', () => {
    const trust = statusTrust(dto({ state: 'stale', signer_masked: null }))
    expect(trust.find((item) => item.key === 'builder')?.done).toBe(false)
    expect(trust.find((item) => item.key === 'sync')?.done).toBe(false)
    expect(trust.find((item) => item.key === 'agent')?.done).toBe(false)
  })

  it('exports the transport-stale warning copy', () => {
    expect(TRANSPORT_STALE_WARNING).toContain('Связь с сервером потеряна')
    expect(TRANSPORT_STALE_WARNING).not.toMatch(/обнови/i)
  })
})
