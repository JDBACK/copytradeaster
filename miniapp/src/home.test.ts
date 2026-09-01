import { describe, expect, it } from 'vitest'
import { homeViewModel, RESUME_CONFIRM_BODY, snapshotLabel, trustSummary } from './home'
import type { DashboardDTO } from './types'

function dashboard(overrides: Partial<DashboardDTO> = {}): DashboardDTO {
  return {
    copy_state: 'paused',
    system_state: 'ready',
    account_masked: '0xABC…DEF',
    signer_masked: '0x111…222',
    risk_percent: '1.5',
    open_positions: 2,
    notification_enabled: true,
    is_admin: false,
    ...overrides,
  }
}

describe('home view model', () => {
  it('active: one primary action, pause', () => {
    const view = homeViewModel(dashboard({ copy_state: 'active' }))
    expect(view.tone).toBe('active')
    expect(view.actionLabel).toBe('pause')
    expect(view.title).toBe('Всё работает')
  })

  it('paused: resume action and protection note', () => {
    const view = homeViewModel(dashboard({ copy_state: 'paused' }))
    expect(view.tone).toBe('paused')
    expect(view.actionLabel).toBe('resume')
    expect(view.subtitle).toContain('Защита')
  })

  it('empty: no action, reconnection hint', () => {
    const view = homeViewModel(dashboard({ copy_state: 'empty' }))
    expect(view.tone).toBe('empty')
    expect(view.actionLabel).toBeNull()
  })

  it('blocked: attention with the typed message', () => {
    const view = homeViewModel(dashboard({ copy_state: 'blocked', message: 'Agent истёк' }))
    expect(view.tone).toBe('attention')
    expect(view.actionLabel).toBeNull()
    expect(view.subtitle).toBe('Agent истёк')
  })

  it('degraded: attention without actions', () => {
    const view = homeViewModel(dashboard({ copy_state: 'degraded' }))
    expect(view.tone).toBe('attention')
    expect(view.actionLabel).toBeNull()
  })

  it('resume confirmation explains the master-in-position entry', () => {
    expect(RESUME_CONFIRM_BODY).toContain('Мастер сейчас в позиции')
  })

  it('never substitutes account updated_at for a missing exchange snapshot', () => {
    expect(snapshotLabel(undefined)).toBe('не получен')
    expect(snapshotLabel(null)).toBe('не получен')
  })
})

describe('trust summary', () => {
  it('marks every step done for a connected, synced account', () => {
    const items = trustSummary(dashboard({ snapshot_captured_at: '2026-09-01T12:41:00Z' }))
    expect(items.map((item) => item.label)).toEqual(['Wallet', 'Agent', 'Builder', 'Sync'])
    expect(items.every((item) => item.done)).toBe(true)
  })

  it('sync is not done without a ready system or a fresh snapshot', () => {
    const items = trustSummary(dashboard({ system_state: 'stale', snapshot_captured_at: null }))
    expect(items.find((item) => item.key === 'sync')?.done).toBe(false)
  })

  it('wallet and agent follow the masked identities, never invented', () => {
    const items = trustSummary(dashboard({ account_masked: null, signer_masked: null }))
    expect(items.find((item) => item.key === 'wallet')?.done).toBe(false)
    expect(items.find((item) => item.key === 'agent')?.done).toBe(false)
  })
})
