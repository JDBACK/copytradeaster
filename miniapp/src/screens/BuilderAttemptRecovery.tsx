import { useCallback, useEffect, useState } from 'react'
import { api, ApiError, type AuthContext } from '../api'
import { Brand, ErrorBox, TrustRail } from '../components/ui'
import { describeError } from '../errors'
import { hapticNotify } from '../telegram'
import type { BuilderOnboardedDTO } from '../types'

type RecoveryState = { kind: 'loading' } | { kind: 'error'; code: string }

export function BuilderAttemptRecovery({
  auth,
  onConnected,
}: {
  auth: AuthContext
  onConnected: (result: BuilderOnboardedDTO) => void
}) {
  const [state, setState] = useState<RecoveryState>({ kind: 'loading' })

  const recover = useCallback(
    async (signal?: AbortSignal) => {
      setState({ kind: 'loading' })
      try {
        const result = await api.builderResumeAttempt(auth, { timeoutMs: 45_000, signal })
        hapticNotify('success')
        onConnected(result)
      } catch (err) {
        if (err instanceof ApiError && err.code === 'aborted') return
        const code = err instanceof ApiError ? err.code : 'network_error'
        hapticNotify('error')
        void api.clientLog(auth, { level: 'error', event: 'attempt_recovery_failed', code })
        setState({ kind: 'error', code })
      }
    },
    [auth, onConnected],
  )

  useEffect(() => {
    const controller = new AbortController()
    // Synchronize the screen with the durable server-side attempt.
    // oxlint-disable-next-line react/set-state-in-effect
    void recover(controller.signal)
    return () => controller.abort()
  }, [recover])

  const errorCopy = state.kind === 'error' ? describeError(state.code) : null

  return (
    <div className="screen">
      <Brand right={<span className="badge-testnet">Aster testnet</span>} />
      <TrustRail active={2} doneCount={2} />

      <section className="panel state-card" aria-live="polite" aria-busy={state.kind === 'loading'}>
        <div className="state-icon" aria-hidden>
          <svg width="38" height="38" viewBox="0 0 24 24" fill="none">
            <path
              d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"
              stroke="var(--champagne)"
              strokeWidth="1.7"
              strokeLinecap="round"
            />
            <path
              d="M19.8 3.8v3.4h-3.4"
              stroke="var(--champagne)"
              strokeWidth="1.7"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </div>
        <h1 className="h1" style={{ margin: 0 }}>
          Проверяем авторизацию
        </h1>
        <p className="lede">
          Подпись уже отправлена. Проверяем Agent на Aster — кошелёк повторно не потребуется.
        </p>
      </section>

      {errorCopy && (
        <>
          <ErrorBox title={errorCopy.title} hint={errorCopy.hint} code={state.kind === 'error' ? state.code : undefined} />
          <button type="button" className="btn btn-primary" onClick={() => void recover()}>
            Повторить проверку
          </button>
        </>
      )}
    </div>
  )
}
