/** ASTER COPY Mini App — composition: session gate → builder wizard →
 * success → follower app shell with four root tabs. */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, ApiError, type AuthContext } from './api'
import { AppShell } from './components/AppShell'
import { BuilderWizard } from './screens/BuilderWizard'
import { BuilderAttemptRecovery } from './screens/BuilderAttemptRecovery'
import { SessionGate, type GateState } from './screens/SessionGate'
import { SuccessScreen } from './screens/SuccessScreen'
import { initData, isTelegram, setupTelegram } from './telegram'
import type { BuilderOnboardedDTO, MiniAppSessionDTO } from './types'
import { sessionDestination } from './session'

type Phase =
  | { kind: 'gate'; state: GateState }
  | { kind: 'wizard' }
  | { kind: 'attempt_recovery' }
  | { kind: 'success'; result: BuilderOnboardedDTO }
  | { kind: 'home' }

const DEV_USER_ID = Number(
  (import.meta.env.VITE_DEV_USER_ID as string | undefined)?.trim() || 0,
)

export default function App() {
  const [phase, setPhase] = useState<Phase>({ kind: 'gate', state: 'loading' })
  const [devMode] = useState(() => !isTelegram() && Boolean(DEV_USER_ID))
  const [gateError, setGateError] = useState<string | undefined>()
  const [isRecovering, setIsRecovering] = useState(false)
  const auth: AuthContext = useMemo(() => {
    const signed = initData()
    if (signed) return { initData: signed }
    return { initData: '', devUserId: DEV_USER_ID || undefined }
  }, [])

  const routeSession = useCallback((session: MiniAppSessionDTO) => {
    setGateError(undefined)
    switch (sessionDestination(session)) {
      case 'runtime_recovery':
        setPhase({ kind: 'gate', state: 'recovery_required' })
        break
      case 'attempt_recovery':
        setPhase({ kind: 'attempt_recovery' })
        break
      case 'home':
        setPhase({ kind: 'home' })
        break
      case 'wizard':
        setPhase({ kind: 'wizard' })
        break
      case 'pending':
        setPhase({ kind: 'gate', state: 'pending' })
        break
      case 'rejected':
        setPhase({ kind: 'gate', state: 'rejected' })
        break
      default:
        setPhase({ kind: 'gate', state: 'invalid' })
    }
  }, [])

  const loadSession = useCallback(async () => {
    if (!isTelegram() && !DEV_USER_ID) {
      setPhase({ kind: 'gate', state: 'invalid' })
      return
    }
    setGateError(undefined)
    try {
      const session = await api.session(auth)
      routeSession(session)
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        if (err.status === 401 || err.code === 'invalid_session') {
          setPhase({ kind: 'gate', state: 'invalid' })
        } else {
          setGateError(err.message)
          setPhase({ kind: 'gate', state: 'error' })
        }
      } else {
        setGateError('Произошла непредвиденная ошибка.')
        setPhase({ kind: 'gate', state: 'error' })
      }
    }
  }, [auth, routeSession])

  const handleRecover = useCallback(async () => {
    setIsRecovering(true)
    setGateError(undefined)
    try {
      const session = await api.builderRecover(auth)
      routeSession(session)
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setGateError(err.message)
      } else {
        setGateError('Не удалось восстановить запуск. Попробуйте снова.')
      }
    } finally {
      setIsRecovering(false)
    }
  }, [auth, routeSession])
  useEffect(() => {
    setupTelegram()
    // oxlint-disable-next-line react/set-state-in-effect
    void loadSession()
  }, [loadSession])

  const devBanner = devMode ? (
    <div className="dev-banner" role="note">
      DEV-режим: пользователь {auth.devUserId}, сессия без подписи Telegram
    </div>
  ) : undefined

  return (
    <>
      {phase.kind !== 'home' && devBanner}
      {phase.kind === 'gate' && (
        <SessionGate
          state={phase.state}
          errorMessage={gateError}
          onRetry={() => void loadSession()}
          onRecover={() => void handleRecover()}
          isRecovering={isRecovering}
        />
      )}
      {phase.kind === 'wizard' && (
        <BuilderWizard auth={auth} onConnected={(result) => setPhase({ kind: 'success', result })} />
      )}
      {phase.kind === 'attempt_recovery' && (
        <BuilderAttemptRecovery
          auth={auth}
          onConnected={(result) => setPhase({ kind: 'success', result })}
        />
      )}
      {phase.kind === 'success' && (
        <SuccessScreen result={phase.result} onHome={() => setPhase({ kind: 'home' })} />
      )}
      {phase.kind === 'home' && (
        <AppShell
          auth={auth}
          banner={devBanner}
          onNotConnected={() => setPhase({ kind: 'wizard' })}
        />
      )}
    </>
  )
}
