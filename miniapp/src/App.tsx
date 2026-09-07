/** ASTER COPY Mini App — composition: session gate → builder wizard →
 * success → follower app shell with four root tabs. */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError, type AuthContext } from './api'
import { AppShell } from './components/AppShell'
import { MiniAppResources } from './resources'
import { ResourceScopeContext } from './resourceScopeContext'
import { BuilderWizard } from './screens/BuilderWizard'
import { BuilderAttemptRecovery } from './screens/BuilderAttemptRecovery'
import { SessionGate, type GateState } from './screens/SessionGate'
import { SuccessScreen } from './screens/SuccessScreen'
import { initData, isTelegram, setupTelegram, launchTradeRef } from './telegram'
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
  const [launchRef] = useState(launchTradeRef)
  const [environment, setEnvironment] = useState<string | null>(null)
  const [phase, setPhase] = useState<Phase>({ kind: 'gate', state: 'loading' })
  const [devMode] = useState(() => !isTelegram() && Boolean(DEV_USER_ID))
  const [gateError, setGateError] = useState<string | undefined>()
  const [isRecovering, setIsRecovering] = useState(false)
  const [accountMasked, setAccountMasked] = useState<string | null>(null)
  const [resources, setResources] = useState<MiniAppResources | null>(null)
  const resourcesRef = useRef<MiniAppResources | null>(null)
  const scopeGenerationRef = useRef(0)
  const lastSessionIdentityRef = useRef<{ userId: number; environment: string } | null>(null)
  const sessionInFlightRef = useRef<Promise<void> | null>(null)
  const auth: AuthContext = useMemo(() => {
    const signed = initData()
    if (signed) return { initData: signed }
    return { initData: '', devUserId: DEV_USER_ID || undefined }
  }, [])

  const disposeResources = useCallback(() => {
    resourcesRef.current?.dispose()
    resourcesRef.current = null
    setResources(null)
  }, [])

  const handleResourceInvalidSession = useCallback(() => {
    // Resource reads run after the initial session gate. A later 401/
    // invalid_session is therefore a session-boundary event, not a screen
    // transport error: dispose the whole DTO scope and return to the gate.
    resourcesRef.current?.dispose()
    resourcesRef.current = null
    setResources(null)
    setPhase({ kind: 'gate', state: 'invalid' })
  }, [])

  const openResourceScope = useCallback(
    ({ userId, environment }: { userId: number; environment: string }) => {
      // The server contract currently has no opaque account scope token.
      // Never key by account_masked: one owner lives only for this confirmed
      // connected-session generation and is explicitly disposed on exit.
      resourcesRef.current?.dispose()
      const generation = ++scopeGenerationRef.current
      const next = new MiniAppResources(
        auth,
        `${userId}:${environment}:session-${generation}`,
        handleResourceInvalidSession,
      )
      resourcesRef.current = next
      setResources(next)
      return next
    },
    [auth, handleResourceInvalidSession],
  )

  const handleNotConnected = useCallback(() => {
    disposeResources()
    setPhase({ kind: 'wizard' })
  }, [disposeResources])

  const routeSession = useCallback((session: MiniAppSessionDTO) => {
    setGateError(undefined)
    setEnvironment(session.environment)
    lastSessionIdentityRef.current = { userId: session.user_id, environment: session.environment }
    if (session.account_masked) {
      setAccountMasked(session.account_masked)
    }
    const destination = sessionDestination(session)
    if (destination === 'home') {
      openResourceScope({ userId: session.user_id, environment: session.environment })
    } else {
      disposeResources()
    }
    switch (destination) {
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
  }, [disposeResources, openResourceScope])

  const loadSession = useCallback(async () => {
    if (sessionInFlightRef.current) return sessionInFlightRef.current
    if (!isTelegram() && !DEV_USER_ID) {
      setPhase({ kind: 'gate', state: 'invalid' })
      return
    }
    const run = (async () => {
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
    })()
    sessionInFlightRef.current = run
    try {
      await run
    } finally {
      if (sessionInFlightRef.current === run) sessionInFlightRef.current = null
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
        <BuilderWizard
          auth={auth}
          onConnected={(result) => {
            if (result.account_masked) {
              setAccountMasked(result.account_masked)
            }
            if (lastSessionIdentityRef.current) openResourceScope(lastSessionIdentityRef.current)
            setPhase({ kind: 'success', result })
          }}
        />
      )}
      {phase.kind === 'attempt_recovery' && (
        <BuilderAttemptRecovery
          auth={auth}
          onConnected={(result) => {
            if (result.account_masked) {
              setAccountMasked(result.account_masked)
            }
            if (lastSessionIdentityRef.current) openResourceScope(lastSessionIdentityRef.current)
            setPhase({ kind: 'success', result })
          }}
        />
      )}
      {phase.kind === 'success' && (
        <SuccessScreen
          result={phase.result}
          onHome={() => {
            if (resourcesRef.current) setPhase({ kind: 'home' })
            else void loadSession()
          }}
        />
      )}
      {phase.kind === 'home' && resources && (
        <ResourceScopeContext.Provider value={resources}>
          <AppShell
            auth={auth}
            resources={resources}
            banner={devBanner}
            launchRef={launchRef}
            environment={environment}
            accountMasked={accountMasked}
            onNotConnected={handleNotConnected}
          />
        </ResourceScopeContext.Provider>
      )}
    </>
  )
}
