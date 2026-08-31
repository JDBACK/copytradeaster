/** Builder onboarding wizard (эталон 01-builder-auth):
 * Wallet → Agent → Builder → Sync, одна combined-подпись approveAgent. */

import { useLayoutEffect, useReducer, useState } from 'react'
import { api, ApiError, type AuthContext } from '../api'
import { describeError } from '../errors'
import { formatDateFromMs, formatFeeRate } from '../format'
import { hapticImpact, hapticNotify, setClosingConfirmation } from '../telegram'
import { Brand, Check, ErrorBox, Row, TrustRail } from '../components/ui'
import {
  initialWizardState,
  railIndex,
  wizardReducer,
} from '../wizard'
import type { BuilderOnboardedDTO } from '../types'
import {
  createInjectedAdapter,
  createWalletConnectAdapter,
  REGISTRATION_CHAIN_ID,
  WalletError,
  walletConnectProjectId,
  type WalletAdapter,
} from '../wallet'

interface Props {
  auth: AuthContext
  onConnected: (result: BuilderOnboardedDTO) => void
}

export function BuilderWizard({ auth, onConnected }: Props) {
  const [state, dispatch] = useReducer(wizardReducer, initialWizardState)
  const [adapter, setAdapter] = useState<WalletAdapter | null>(null)
  const [busyMessage, setBusyMessage] = useState<string | null>(null)
  const railDone = state.step === 'sync' ? 3 : railIndex(state.step)

  useLayoutEffect(() => {
    // Signing is a destructive-to-abort action: warn before accidental close.
    setClosingConfirmation(state.step === 'review' || state.step === 'signing')
    return () => setClosingConfirmation(false)
  }, [state.step])

  async function resolveAdapter(): Promise<WalletAdapter> {
    if (adapter) return adapter
    const wcProjectId = walletConnectProjectId()
    const injected = createInjectedAdapter()
    if (injected) {
      setAdapter(injected)
      return injected
    }
    if (wcProjectId) {
      const wc = await createWalletConnectAdapter(wcProjectId)
      setAdapter(wc)
      return wc
    }
    throw new WalletError('no_wallet')
  }

  async function connectWallet() {
    hapticImpact('light')
    setBusyMessage('Подключаем кошелёк…')
    dispatch({ type: 'connecting' })
    try {
      const wallet = await resolveAdapter()
      const address = await wallet.connect()
      dispatch({ type: 'wallet_connected', address })
      await runPrepare(address, wallet)
    } catch (err) {
      hapticNotify('error')
      dispatch({
        type: 'preparation_failed',
        code: err instanceof WalletError ? err.code : 'wallet_unavailable',
        status: 0,
      })
    } finally {
      setBusyMessage(null)
    }
  }

  async function runPrepare(address: string, wallet: WalletAdapter) {
    setBusyMessage('Готовим данные авторизации…')
    dispatch({ type: 'preparing' })
    try {
      await wallet.switchToRegistrationChain()
      const dto = await api.builderPrepare(auth, address)
      hapticImpact('medium')
      dispatch({ type: 'prepared', prepare: dto, address })
    } catch (err) {
      hapticNotify('error')
      if (err instanceof ApiError) {
        dispatch({ type: 'preparation_failed', code: err.code, status: err.status })
      } else {
        dispatch({ type: 'preparation_failed', code: 'network_error', status: 0 })
      }
    } finally {
      setBusyMessage(null)
    }
  }

  async function sign() {
    if (!state.prepare || !state.address) return
    hapticImpact('medium')
    dispatch({ type: 'signing' })
    setBusyMessage('Ждём подпись в кошельке…')
    let signature = ''
    try {
      const wallet = await resolveAdapter()
      signature = await wallet.signTypedData(state.address, state.prepare.eip712)
    } catch (err) {
      hapticNotify('error')
      dispatch({
        type: 'signing_failed',
        code: err instanceof WalletError ? err.code : 'wallet_unavailable',
      })
      setBusyMessage(null)
      return
    }
    setBusyMessage('Проверяем на Aster и подключаем счёт…')
    try {
      const result = await api.builderSubmit(auth, state.prepare.pending_id, signature)
      hapticNotify('success')
      onConnected(result)
    } catch (err) {
      hapticNotify('error')
      if (err instanceof ApiError) {
        dispatch({ type: 'submit_failed', code: err.code, status: err.status })
      } else {
        dispatch({ type: 'submit_failed', code: 'network_error', status: 0 })
      }
    } finally {
      setBusyMessage(null)
    }
  }

  const error = state.errorCode ? describeError(state.errorCode) : null
  const prepare = state.prepare

  return (
    <div className="screen">
      <Brand right={<span className="badge-testnet">Aster testnet</span>} />

      <h1 className="h1">Подключение Aster</h1>
      <TrustRail active={railIndex(state.step)} doneCount={railDone} />

      {error && (
        <ErrorBox title={error.title} hint={error.hint}>
          {state.errorCode !== 'owner_conflict' && state.step === 'wallet' && (
            <button type="button" className="linklike" onClick={() => dispatch({ type: 'restart' })}>
              Начать заново
            </button>
          )}
          {state.step === 'review' && (
            <button type="button" className="linklike" onClick={() => dispatch({ type: 'dismiss_error' })}>
              Понятно
            </button>
          )}
        </ErrorBox>
      )}

      {state.step === 'wallet' && (
        <WalletStep
          busy={state.busy}
          busyMessage={busyMessage}
          address={state.address}
          onConnect={connectWallet}
        />
      )}

      {prepare && state.step !== 'wallet' && (
        <>
          <section className="panel">
            <div className="h2">Разрешения Agent</div>
            <Row
              label={
                <>
                  <Check ok={prepare.can_perp_trade} /> Perps-торговля
                </>
              }
              value={prepare.can_perp_trade ? 'включена' : 'выключена'}
            />
            <Row
              label={
                <>
                  <Check ok={!prepare.can_withdraw} /> Вывод средств
                </>
              }
              value={prepare.can_withdraw ? 'включён' : 'запрещён'}
              valueClass={prepare.can_withdraw ? 'tone-danger' : undefined}
            />
            <Row label="Кошелёк" value={prepare.user_masked} />
            <Row label="Agent" value={prepare.agent_masked} />
          </section>

          <section className="panel">
            <div className="h2">Builder и комиссия</div>
            <Row label="Builder" value={prepare.builder_name} />
            <Row label="Адрес Builder" value={prepare.builder_masked} />
            <Row label="Комиссия Builder" value={formatFeeRate(prepare.max_fee_rate)} />
            <Row label="Действует до" value={formatDateFromMs(prepare.agent_expires_at_ms)} />
          </section>

          {state.step === 'review' && (
            <div className="btn-stack">
              <button type="button" className="btn btn-primary" disabled={state.busy} onClick={sign}>
                {busyMessage ?? 'Подписать авторизацию'}
              </button>
              <p className="lede" style={{ textAlign: 'center' }}>
                Одна подпись в кошельке подтверждает Agent и Builder. Основной ключ не покидает
                кошелёк. Подпись идёт в сети BSC (сеть {REGISTRATION_CHAIN_ID}).
              </p>
            </div>
          )}

          {state.step === 'signing' && (
            <div className="info-box">Подпишите запрос в кошельке — он ждёт подтверждения.</div>
          )}

          {state.step === 'sync' && (
            <div className="info-box">
              Проверяем Agent и Builder на Aster, читаем баланс, позиции и ордера. Обычно это
              несколько секунд.
            </div>
          )}
        </>
      )}

      {state.step === 'wallet' && (
        <div className="btn-stack">
          <button
            type="button"
            className="btn btn-primary"
            disabled={state.busy}
            onClick={connectWallet}
          >
            {busyMessage ?? 'Подключить кошелёк'}
          </button>
        </div>
      )}

      <p className="footer-note">
        Он нужен только для авторизации Agent и Builder. Основной ключ не передаётся сервису.
      </p>
    </div>
  )
}

function WalletStep({
  busy,
  busyMessage,
  address,
  onConnect,
}: {
  busy: boolean
  busyMessage: string | null
  address: string | null
  onConnect: () => void
}) {
  const hasInjected = Boolean((window as { ethereum?: unknown }).ethereum)
  const wcReady = Boolean(walletConnectProjectId())
  return (
    <>
      <section className="panel">
        <div className="h2">Подключите основной кошелёк</div>
        <p className="lede">
          Он нужен только для авторизации Agent и Builder. Основной ключ не передаётся сервису.
        </p>
        <Row label="Сеть подписи" value={`BNB Smart Chain (${REGISTRATION_CHAIN_ID})`} />
        <Row label="Комиссия Builder" value={address ? 'см. ниже' : 'покажем после подключения'} />
      </section>
      {address && (
        <div className="info-box">
          Кошелёк подключен: <span className="num">{short(address)}</span>. Нажмите «Продолжить»,
          чтобы получить данные авторизации.
        </div>
      )}
      <div className="btn-stack">
        <button type="button" className="btn btn-primary" disabled={busy} onClick={onConnect}>
          {busyMessage ?? (address ? 'Продолжить' : 'Подключить кошелёк')}
        </button>
      </div>
      {!hasInjected && !wcReady && (
        <div className="info-box">
          В этом окружении не найден EVM-кошелёк. Откройте мини-ап в браузере с MetaMask, либо из
          Telegram — подключение через WalletConnect появится, когда будет указан проект.
        </div>
      )}
    </>
  )
}

function short(address: string): string {
  return address.length > 14 ? `${address.slice(0, 6)}…${address.slice(-4)}` : address
}
