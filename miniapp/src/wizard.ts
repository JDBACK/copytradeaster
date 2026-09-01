/** Pure wizard state machine (Builder onboarding, trust rail
 * Wallet → Agent → Builder → Sync).  No React, fully unit-testable. */

import type { BuilderPrepareDTO } from './types'

export type WizardStep = 'wallet' | 'review' | 'signing' | 'sync'

export interface WizardState {
  step: WizardStep
  address: string | null
  prepare: BuilderPrepareDTO | null
  busy: boolean
  errorCode: string | null
  errorStatus: number | null
}

export const initialWizardState: WizardState = {
  step: 'wallet',
  address: null,
  prepare: null,
  busy: false,
  errorCode: null,
  errorStatus: null,
}

export type WizardEvent =
  | { type: 'connecting' }
  | { type: 'wallet_connected'; address: string }
  | { type: 'preparing' }
  | { type: 'prepared'; prepare: BuilderPrepareDTO; address: string }
  | { type: 'preparation_failed'; code: string; status: number }
  | { type: 'signing' }
  | { type: 'signed'; signature: string }
  | { type: 'signing_failed'; code: string }
  | { type: 'submitting' }
  | { type: 'submit_failed'; code: string; status: number }
  | { type: 'restart' }
  | { type: 'dismiss_error' }

export function wizardReducer(state: WizardState, event: WizardEvent): WizardState {
  switch (event.type) {
    case 'connecting':
    case 'preparing':
      return { ...state, busy: true, errorCode: null, errorStatus: null }
    case 'wallet_connected':
      return { ...state, busy: false, address: event.address }
    case 'prepared':
      return {
        ...state,
        busy: false,
        step: 'review',
        address: event.address,
        prepare: event.prepare,
        errorCode: null,
        errorStatus: null,
      }
    case 'preparation_failed':
      return {
        ...state,
        busy: false,
        errorCode: event.code,
        errorStatus: event.status,
        // Owner-conflict is terminal for this flow: the account exists and
        // must be disconnected in the bot first (decision #11).
        step: event.code === 'owner_conflict' || event.status === 401 ? 'wallet' : state.step,
      }
    case 'signing':
      return { ...state, busy: true, step: 'signing', errorCode: null, errorStatus: null }
    case 'signed':
      return { ...state, busy: true, step: 'sync' }
    case 'signing_failed':
      return { ...state, busy: false, step: 'review', errorCode: event.code, errorStatus: null }
    case 'submitting':
      return state
    case 'submit_failed':
      return {
        ...state,
        busy: false,
        errorCode: event.code,
        errorStatus: event.status,
        step: event.code === 'approval_expired' ? 'wallet' : 'review',
      }
    case 'restart':
      return { ...initialWizardState }
    case 'dismiss_error':
      return { ...state, errorCode: null, errorStatus: null }
    default:
      return state
  }
}

/** Trust-rail progress: wallet → agent → builder → sync, UX plan §7.2. */
export type RailIndex = 0 | 1 | 2 | 3

export function railIndex(step: WizardStep): RailIndex {
  switch (step) {
    case 'wallet':
      return 0
    case 'review':
      return 1
    case 'signing':
      return 2
    case 'sync':
      return 3
  }
}
