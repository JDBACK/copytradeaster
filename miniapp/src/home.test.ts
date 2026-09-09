import { describe, expect, it } from 'vitest'
import {
  homeViewModel,
  RESUME_CONFIRM_BODY,
  RESUME_CONFIRM_FLAT_BODY,
  resumeConfirmBody,
  snapshotLabel,
  trustSummary,
} from './home'
import type { DashboardDTO, ResumeContextDTO } from './types'

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
    expect(view.title).toBe('Копирование включено')
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

  it('resume confirmation picks its copy from the proven resume context', () => {
    expect(
      resumeConfirmBody({ snapshot_freshness: 'fresh', master_in_position: false, permission_ok: true }),
    ).toBe(RESUME_CONFIRM_FLAT_BODY)
    expect(
      resumeConfirmBody({ snapshot_freshness: 'fresh', master_in_position: true, permission_ok: true }),
    ).toBe(RESUME_CONFIRM_BODY)
  })

  it('unknown or stale resume context stays neutral and never claims flat', () => {
    const contexts: Array<ResumeContextDTO | null | undefined> = [
      undefined,
      null,
      { snapshot_freshness: 'fresh', master_in_position: null, permission_ok: null },
      { snapshot_freshness: 'stale', master_in_position: false, permission_ok: true },
      { snapshot_freshness: 'unavailable', master_in_position: true, permission_ok: true },
    ]
    for (const context of contexts) {
      const body = resumeConfirmBody(context)
      expect(body).not.toBe(RESUME_CONFIRM_FLAT_BODY)
      expect(body).not.toBe(RESUME_CONFIRM_BODY)
      expect(body).not.toContain('Мастер сейчас в позиции')
      expect(body).toContain('После проверки актуального состояния')
    }
  })

  it('never substitutes account updated_at for a missing exchange snapshot', () => {
    expect(snapshotLabel(undefined)).toBe('не получен')
    expect(snapshotLabel(null)).toBe('не получен')
  })
})

describe('trust summary', () => {
  it('marks wallet and agent done, but builder remains pending before P2.6', () => {
    const items = trustSummary(dashboard({ snapshot_captured_at: '2026-09-01T12:41:00Z' }))
    expect(items.map((item) => item.label)).toEqual(['Wallet', 'Agent', 'Builder', 'Sync'])
    expect(items.find((item) => item.key === 'wallet')?.done).toBe(true)
    expect(items.find((item) => item.key === 'agent')?.done).toBe(true)
    expect(items.find((item) => item.key === 'builder')?.done).toBe(false)
    expect(items.find((item) => item.key === 'sync')?.done).toBe(true)
  })

  it('direct/unverified builder does not receive success', () => {
    const items = trustSummary(dashboard({ snapshot_captured_at: '2026-09-01T12:41:00Z' }))
    expect(items.find((item) => item.key === 'builder')?.done).toBe(false)
  })

  it('stale system with snapshot does not get successful sync', () => {
    const items = trustSummary(dashboard({ system_state: 'stale', snapshot_captured_at: '2026-09-01T12:41:00Z' }))
    expect(items.find((item) => item.key === 'sync')?.done).toBe(false)
  })

  it('ready system without snapshot does not get successful sync', () => {
    const items = trustSummary(dashboard({ system_state: 'ready', snapshot_captured_at: null }))
    expect(items.find((item) => item.key === 'sync')?.done).toBe(false)
  })

  it('ready system with snapshot gets successful sync', () => {
    const items = trustSummary(dashboard({ system_state: 'ready', snapshot_captured_at: '2026-09-01T12:41:00Z' }))
    expect(items.find((item) => item.key === 'sync')?.done).toBe(true)
  })

  it('wallet and agent follow the masked identities, never invented', () => {
    const items = trustSummary(dashboard({ account_masked: null, signer_masked: null }))
    expect(items.find((item) => item.key === 'wallet')?.done).toBe(false)
    expect(items.find((item) => item.key === 'agent')?.done).toBe(false)
  })
})
