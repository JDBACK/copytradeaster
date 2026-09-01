import { describe, expect, it } from 'vitest'
import { sessionDestination } from './session'
import type { MiniAppSessionDTO } from './types'

function session(overrides: Partial<MiniAppSessionDTO> = {}): MiniAppSessionDTO {
  return {
    user_id: 777,
    access_state: 'approved',
    environment: 'testnet',
    connection_state: 'empty',
    account_connected: false,
    ...overrides,
  }
}

describe('Mini App session routing', () => {
  it.each(['APPROVAL_SENT', 'APPROVED'] as const)(
    'routes %s to server-owned attempt recovery',
    (onboardingState) => {
      expect(
        sessionDestination(
          session({ connection_state: 'onboarding', onboarding_state: onboardingState }),
        ),
      ).toBe('attempt_recovery')
    },
  )

  it('keeps PREPARED in the wallet-signing wizard', () => {
    expect(
      sessionDestination(
        session({ connection_state: 'onboarding', onboarding_state: 'PREPARED' }),
      ),
    ).toBe('wizard')
  })

  it('routes committed runtime recovery separately', () => {
    expect(sessionDestination(session({ connection_state: 'recovery_required' }))).toBe(
      'runtime_recovery',
    )
  })
})
