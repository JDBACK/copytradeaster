import type { MiniAppSessionDTO } from './types'

export type SessionDestination =
  | 'runtime_recovery'
  | 'attempt_recovery'
  | 'home'
  | 'wizard'
  | 'pending'
  | 'rejected'
  | 'invalid'

export function sessionDestination(session: MiniAppSessionDTO): SessionDestination {
  switch (session.access_state) {
    case 'approved':
      if (session.connection_state === 'recovery_required') return 'runtime_recovery'
      if (session.account_connected || session.connection_state === 'connected') return 'home'
      if (
        session.connection_state === 'onboarding' &&
        (session.onboarding_state === 'APPROVAL_SENT' || session.onboarding_state === 'APPROVED')
      ) {
        return 'attempt_recovery'
      }
      return 'wizard'
    case 'pending':
      return 'pending'
    case 'rejected':
      return 'rejected'
    default:
      return 'invalid'
  }
}
