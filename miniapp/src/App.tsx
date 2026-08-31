/** ASTER COPY Mini App — part-1 composition:
 * session gate → builder wizard → success → minimal home. */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, ApiError, type AuthContext } from './api'
import { HomeScreen } from './screens/HomeScreen'
import { BuilderWizard } from './screens/BuilderWizard'
import { SessionGate, type GateState } from './screens/SessionGate'
import { SuccessScreen } from './screens/SuccessScreen'
import { initData, isTelegram, setupTelegram } from './telegram'
import type { BuilderOnboardedDTO, MiniAppSessionDTO } from './types'

type Phase =
  | { kind: 'gate'; state: GateState }
  | { kind: 'wizard' }
  | { kind: 'success'; result: BuilderOnboardedDTO }
  | { kind: 'home' }

const DEV_USER_ID = Number(
  (import.meta.env.VITE_DEV_USER_ID as string | undefined)?.trim() || 0,
)

export default function App() {
  const [phase, setPhase] = useState<Phase>({ kind: 'gate', state: 'loading' })
  const [devMode, setDevMode] = useState(false)

  const auth: AuthContext = useMemo(() => {
    const signed = initData()
    if (signed) return { initData: signed }
    return { initData: '', devUserId: DEV_USER_ID || undefined }
  }, [])

  const loadSession = useCallback(async () => {
    if (!isTelegram() && !DEV_USER_ID) {
      setPhase({ kind: 'gate', state: 'invalid' })
      return
    }
    try {
      const session = await api.session(auth)
      routeSession(session)
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setPhase({ kind: 'gate', state: 'invalid' })
      } else {
        setPhase({ kind: 'gate', state: 'invalid' })
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auth])

  function routeSession(session: MiniAppSessionDTO) {
    switch (session.access_state) {
      case 'approved':
        if (session.account_connected) {
          setPhase({ kind: 'home' })
        } else {
          setPhase({ kind: 'wizard' })
        }
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
  }

  useEffect(() => {
    setupTelegram()
    setDevMode(!isTelegram() && Boolean(DEV_USER_ID))
    void loadSession()
  }, [loadSession])

  return (
    <>
      {devMode && (
        <div className="dev-banner" role="note">
          DEV-режим: пользователь {auth.devUserId}, сессия без подписи Telegram
        </div>
      )}
      {phase.kind === 'gate' && <SessionGate state={phase.state} />}
      {phase.kind === 'wizard' && (
        <BuilderWizard auth={auth} onConnected={(result) => setPhase({ kind: 'success', result })} />
      )}
      {phase.kind === 'success' && (
        <SuccessScreen result={phase.result} onHome={() => setPhase({ kind: 'home' })} />
      )}
      {phase.kind === 'home' && <HomeScreen auth={auth} onNotConnected={() => setPhase({ kind: 'wizard' })} />}
    </>
  )
}
