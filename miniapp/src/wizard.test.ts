import { describe, expect, it } from 'vitest'
import type { BuilderPrepareDTO } from './types'
import { initialWizardState, railIndex, wizardReducer } from './wizard'

const prepare: BuilderPrepareDTO = {
  pending_id: 'pid',
  user_masked: '0xABC…DEF',
  agent_masked: '0x111…222',
  builder_masked: '0x257…6EF',
  builder_name: 'copytradeaster',
  max_fee_rate: '0.0001',
  signature_plan: 'combined_approve_agent',
  can_perp_trade: true,
  can_withdraw: false,
  agent_expires_at_ms: 1_800_000_000_000,
  approval_expires_at_ms: 1_700_000_600_000,
  eip712: { primaryType: 'ApproveAgent' },
}

describe('wizard reducer', () => {
  it('walks wallet -> review on a successful prepare', () => {
    let state = wizardReducer(initialWizardState, { type: 'connecting' })
    expect(state.busy).toBe(true)
    state = wizardReducer(state, { type: 'wallet_connected', address: '0xabc' })
    expect(state.address).toBe('0xabc')
    state = wizardReducer(state, { type: 'preparing' })
    state = wizardReducer(state, { type: 'prepared', prepare, address: '0xabc' })
    expect(state.step).toBe('review')
    expect(state.prepare?.pending_id).toBe('pid')
    expect(state.busy).toBe(false)
  })

  it('returns to wallet on owner conflict (terminal for this flow)', () => {
    let state = wizardReducer(initialWizardState, { type: 'prepared', prepare, address: '0xabc' })
    state = wizardReducer(state, {
      type: 'preparation_failed',
      code: 'owner_conflict',
      status: 400,
    })
    expect(state.step).toBe('wallet')
    expect(state.errorCode).toBe('owner_conflict')
  })

  it('keeps review and records the code when the wallet rejects the signature', () => {
    let state = wizardReducer(initialWizardState, { type: 'prepared', prepare, address: '0xabc' })
    state = wizardReducer(state, { type: 'signing' })
    expect(state.step).toBe('signing')
    state = wizardReducer(state, { type: 'signing_failed', code: 'wallet_rejected' })
    expect(state.step).toBe('review')
    expect(state.errorCode).toBe('wallet_rejected')
    expect(state.busy).toBe(false)
  })

  it('moves to sync only after a signature exists', () => {
    let state = wizardReducer(initialWizardState, { type: 'prepared', prepare, address: '0xabc' })
    state = wizardReducer(state, { type: 'signed', signature: '0xsig' })
    expect(state.step).toBe('sync')
    expect(state.busy).toBe(true)
  })

  it('restarts from wallet when the approval window expired', () => {
    let state = wizardReducer(initialWizardState, { type: 'prepared', prepare, address: '0xabc' })
    state = wizardReducer(state, { type: 'signed', signature: '0xsig' })
    state = wizardReducer(state, { type: 'submit_failed', code: 'approval_expired', status: 400 })
    expect(state.step).toBe('wallet')
    state = wizardReducer(state, { type: 'restart' })
    expect(state).toEqual(initialWizardState)
  })

  it('stays on review for other submit failures', () => {
    let state = wizardReducer(initialWizardState, { type: 'prepared', prepare, address: '0xabc' })
    state = wizardReducer(state, { type: 'signed', signature: '0xsig' })
    state = wizardReducer(state, { type: 'submit_failed', code: 'agent_not_confirmed', status: 400 })
    expect(state.step).toBe('review')
  })

  it('maps rail positions for the trust rail', () => {
    expect(railIndex('wallet')).toBe(0)
    expect(railIndex('review')).toBe(1)
    expect(railIndex('signing')).toBe(2)
    expect(railIndex('sync')).toBe(3)
  })
})
