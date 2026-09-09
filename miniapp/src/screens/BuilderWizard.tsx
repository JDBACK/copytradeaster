/** Builder onboarding wizard (эталон 01-builder-auth):
 * Wallet → Agent → Builder → Sync, одна combined-подпись approveAgent.
 *
 * Every failure is reported to the server log (fire-and-forget client log)
 * so acceptance issues are visible to the operator without user screenshots.
 */

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
  type WizardEvent,
  type WizardState,
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

  function report(event: string, err: unknown, fallbackCode: string) {
    const code = err instanceof WalletError ? err.code : fallbackCode
    void api
      .clientLog(auth, { level: 'error', event, code })
      .catch(() => {})
    return { code }
  }

  function reportApi(event: string, err: ApiError) {
    void api
      .clientLog(auth, { level: 'error', event, code: err.code })
      .catch(() => {})
  }
  function emit(event: WizardEvent) {
    dispatch(event)
  }

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
    emit({ type: 'connecting' })
    try {
      const wallet = await resolveAdapter()
      const address = await wallet.connect()
      emit({ type: 'wallet_connected', address })
      await runPrepare(address, wallet)
    } catch (err) {
      hapticNotify('error')
      const { code } = report('wallet_connect_failed', err, 'wallet_unavailable')
      emit({ type: 'preparation_failed', code, status: 0 })
    } finally {
      setBusyMessage(null)
    }
  }

  async function runPrepare(address: string, wallet: WalletAdapter) {
    setBusyMessage('Готовим данные авторизации…')
    emit({ type: 'preparing' })
    try {
      await wallet.switchToRegistrationChain()
      const dto = await api.builderPrepare(auth, address)
      hapticImpact('medium')
      emit({ type: 'prepared', prepare: dto, address })
    } catch (err) {
      hapticNotify('error')
      if (err instanceof ApiError) {
        reportApi('prepare_failed', err)
        emit({ type: 'preparation_failed', code: err.code, status: err.status })
      } else {
        const { code } = report('prepare_failed', err, 'network_error')
        emit({ type: 'preparation_failed', code, status: 0 })
      }
    } finally {
      setBusyMessage(null)
    }
  }

  async function sign() {
    if (!state.prepare || !state.address || !state.prepare.pending_id) return
    hapticImpact('medium')
    emit({ type: 'signing' })
    setBusyMessage('Ждём подпись в кошельке…')
    let signature = ''
    try {
      const wallet = await resolveAdapter()
      signature = await wallet.signTypedData(state.address, state.prepare.eip712)
    } catch (err) {
      hapticNotify('error')
      const { code } = report('sign_failed', err, 'wallet_unavailable')
      emit({ type: 'signing_failed', code })
      setBusyMessage(null)
      return
    }

    emit({ type: 'signed', signature })
    setBusyMessage('Проверяем Agent и Builder на Aster…')
    try {
      const result = await api.builderSubmit(auth, state.prepare.pending_id, signature)
      hapticNotify('success')
      if (adapter && typeof adapter.disconnect === 'function') {
        void adapter.disconnect().catch(() => {})
      }
      onConnected(result)
    } catch (err) {
      hapticNotify('error')
      if (err instanceof ApiError) {
        reportApi('submit_failed', err)
        emit({ type: 'submit_failed', code: err.code, status: err.status })
      } else {
        const { code } = report('submit_failed', err, 'network_error')
        emit({ type: 'submit_failed', code, status: 0 })
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
        <ErrorBox title={error.title} hint={error.hint} code={state.errorCode ?? undefined}>
          {state.step === 'wallet' && state.errorCode !== 'owner_conflict' && (
            <button type="button" className="linklike" onClick={() => emit({ type: 'restart' })}>
              Начать заново
            </button>
          )}
          {state.step === 'review' && (
            <button type="button" className="linklike" onClick={() => emit({ type: 'dismiss_error' })}>
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
                  <Check ok={prepare.can_perp_trade === true} /> Торговля фьючерсами
                </>
              }
            value={prepare.can_perp_trade == null ? 'Не удалось проверить' : prepare.can_perp_trade ? 'Включена' : 'Выключена'}
            />
            <Row
              label={
                <>
                  <Check ok={prepare.can_withdraw === false} /> Вывод средств
                </>
              }
            value={prepare.can_withdraw == null ? 'Не удалось проверить' : prepare.can_withdraw ? 'Включён' : 'Запрещён'}
            valueClass={prepare.can_withdraw ? 'tone-danger' : undefined}
            />
            <Row label="Кошелёк" value={prepare.user_masked ?? '—'} />
            <Row label="Agent" value={prepare.agent_masked ?? '—'} />
          </section>

          <section className="panel">
            <div className="h2">Builder и комиссия</div>
            <Row label="Builder" value={prepare.builder_name || 'copytradeaster'} />
            <Row label="Адрес Builder" value={prepare.builder_masked ?? '—'} />
            <Row label="Комиссия Builder" value={prepare.max_fee_rate ? formatFeeRate(prepare.max_fee_rate) : 'Условия не получены'} />
            <Row label="Действует до" value={formatDateFromMs(prepare.agent_expires_at_ms ?? 0)} />
          </section>

          {state.step === 'review' && (
            <div className="btn-stack">
              <button type="button" className="btn btn-primary" disabled={state.busy || !prepare.max_fee_rate || prepare.can_perp_trade !== true || prepare.can_withdraw !== false} onClick={sign}>
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

      <p className="footer-note">
        Основной приватный ключ остаётся в вашем кошельке и никогда не передаётся сервису.
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
          Для безопасной авторизации Agent и Builder на бирже Aster без передачи приватного ключа.
        </p>
        <Row label="Сеть подписи" value={`BNB Smart Chain (${REGISTRATION_CHAIN_ID})`} />
        <Row
          label="Комиссия Builder"
          value={address ? 'На следующем шаге' : 'Покажем после подключения'}
        />
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

export type { WizardState, WizardEvent }
