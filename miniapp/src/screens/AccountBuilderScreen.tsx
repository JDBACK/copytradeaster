/** Account + Builder screen (nested, Stage 3 part 2 P2.6, эталон 07-account-builder).
 *
 * Read-only attestation slice over the authoritative backend AccountBuilderDTO:
 * masked account/Agent/Builder identities, the actual authorized fee cap, Agent
 * permissions/expiry and the backend-controlled typed state.  The last
 * successfully received DTO stays on screen in local component state only
 * (no store): a failed manual refresh marks the view transport-stale instead
 * of destroying data, and a late request never overwrites a newer one
 * (sequence guard).  Stale never renders as verified.
 *
 * Actions are strictly backend-driven:
 * - «Перепроверить разрешения» runs the signed Agent + Builder re-check;
 * - «Переавторизовать» appears only when backend remediation == 'reauthorize'
 *   and reuses the Part 1 wallet/EIP-712 flow for the current signer
 *   (same-signer re-approval, no rotation, account stays paused);
 * - «Отключить аккаунт» is destructive, local-only (Aster-side Agent/Builder
 *   authorization is NOT revoked), and goes through the existing
 *   begin/confirm confirmation endpoints.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, type AuthContext } from '../api'
import { Brand, ErrorBox, Row, Sheet } from '../components/ui'
import { describeError } from '../errors'
import { formatDateFromMs, formatFeeRate, formatTime } from '../format'
import { hapticImpact, hapticNotify } from '../telegram'
import {
  createInjectedAdapter,
  createWalletConnectAdapter,
  walletConnectProjectId,
  WalletError,
  type WalletAdapter,
} from '../wallet'
import {
  DISCONNECT_CONFIRM_BODY,
  DISCONNECT_CONFIRM_TITLE,
  isAccountBuilderVerified,
  reasonCopy,
  stateCopy,
} from '../accountBuilder'
import { TRANSPORT_STALE_WARNING } from '../systemStatus'
import type { AccountBuilderDTO, BuilderPrepareDTO, ConfirmationDTO } from '../types'

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; dto: AccountBuilderDTO; transportStale: boolean }
  | { kind: 'error'; code: string }

type ReauthState =
  | { kind: 'idle' }
  | { kind: 'preparing' }
  | { kind: 'signing'; prepare: BuilderPrepareDTO }
  | { kind: 'submitting' }
  | { kind: 'failed'; code: string }

export function AccountBuilderScreen({
  auth,
  accountMasked,
  onNotConnected,
}: {
  auth: AuthContext
  accountMasked?: string | null
  onNotConnected: () => void
}) {
  const [load, setLoad] = useState<LoadState>({ kind: 'loading' })
  const [refreshing, setRefreshing] = useState(false)
  const [reauth, setReauth] = useState<ReauthState>({ kind: 'idle' })
  const [pendingDisconnect, setPendingDisconnect] = useState<ConfirmationDTO | null>(null)
  const [acting, setActing] = useState(false)
  const [fatalError, setFatalError] = useState<string | null>(null)
  const seq = useRef(0)
  const adapterRef = useRef<WalletAdapter | null>(null)

  const loadInitial = useCallback(async () => {
    const my = ++seq.current
    try {
      const dto = await api.accountBuilder(auth, false)
      if (seq.current !== my) return
      setLoad({ kind: 'ready', dto, transportStale: false })
    } catch (err) {
      if (seq.current !== my) return
      setLoad({ kind: 'error', code: err instanceof ApiError ? err.code : 'network_error' })
    }
  }, [auth])

  useEffect(() => {
    // Initial fetch only: loadInitial awaits before its first setState.
    // oxlint-disable-next-line react/set-state-in-effect
    void loadInitial()
  }, [loadInitial])

  const refresh = useCallback(async () => {
    if (load.kind !== 'ready' || refreshing) return
    const previous = load.dto
    const my = ++seq.current
    hapticImpact('light')
    setRefreshing(true)
    try {
      const dto = await api.accountBuilder(auth, true)
      // A late response must never overwrite a newer refresh result.
      if (seq.current !== my) return
      setLoad({ kind: 'ready', dto, transportStale: false })
    } catch {
      if (seq.current !== my) return
      hapticNotify('error')
      setLoad({ kind: 'ready', dto: previous, transportStale: true })
    } finally {
      if (seq.current === my) setRefreshing(false)
    }
  }, [auth, load, refreshing])

  async function resolveAdapter(): Promise<WalletAdapter> {
    if (adapterRef.current) return adapterRef.current
    const injected = createInjectedAdapter()
    if (injected) {
      adapterRef.current = injected
      return injected
    }
    const wcProjectId = walletConnectProjectId()
    if (wcProjectId) {
      const wc = await createWalletConnectAdapter(wcProjectId)
      adapterRef.current = wc
      return wc
    }
    throw new WalletError('no_wallet')
  }

  const reportApi = useCallback(
    (event: string, err: ApiError) => {
      void api.clientLog(auth, { level: 'error', event, code: err.code }).catch(() => {})
    },
    [auth],
  )
  const startReauthorize = useCallback(async () => {
    if (load.kind !== 'ready' || load.dto.remediation !== 'reauthorize') return
    if (reauth.kind === 'preparing' || reauth.kind === 'signing' || reauth.kind === 'submitting') {
      return
    }
    hapticImpact('medium')
    setReauth({ kind: 'preparing' })
    try {
      const wallet = await resolveAdapter()
      await wallet.switchToRegistrationChain()
      await wallet.connect()
      const prepare = await api.reauthorizePrepare(auth)
      if (!prepare.pending_id || !prepare.eip712) {
        throw new ApiError(0, 'network_error', 'Некорректный ответ сервера.')
      }
      setReauth({ kind: 'signing', prepare })
    } catch (err) {
      hapticNotify('error')
      const code =
        err instanceof ApiError ? err.code : err instanceof WalletError ? err.code : 'network_error'
      if (err instanceof ApiError) reportApi('reauthorize_prepare_failed', err)
      setReauth({ kind: 'failed', code })
    }
  }, [auth, load, reauth.kind, reportApi])

  const signReauthorize = useCallback(async () => {
    if (reauth.kind !== 'signing') return
    const { prepare } = reauth
    if (!prepare.pending_id || !prepare.eip712) return
    hapticImpact('medium')
    setReauth({ kind: 'submitting' })
    try {
      const wallet = await resolveAdapter()
      const address = await wallet.connect()
      const signature = await wallet.signTypedData(address, prepare.eip712)
      await api.reauthorizeSubmit(auth, prepare.pending_id, signature)
      hapticNotify('success')
      setReauth({ kind: 'idle' })
      // The proof was rebuilt server-side: re-read the authoritative DTO.
      const dto = await api.accountBuilder(auth, true)
      setLoad({ kind: 'ready', dto, transportStale: false })
    } catch (err) {
      hapticNotify('error')
      const code =
        err instanceof ApiError ? err.code : err instanceof WalletError ? err.code : 'network_error'
      if (err instanceof ApiError) reportApi('reauthorize_submit_failed', err)
      setReauth({ kind: 'failed', code })
    }
  }, [auth, reauth, reportApi])

  const startDisconnect = useCallback(async () => {
    if (acting || pendingDisconnect) return
    hapticImpact('medium')
    setActing(true)
    setFatalError(null)
    try {
      const pending = await api.beginConfirmation(auth, 'disconnect')
      setPendingDisconnect(pending)
    } catch (err) {
      hapticNotify('error')
      setFatalError(err instanceof ApiError ? err.code : 'network_error')
    } finally {
      setActing(false)
    }
  }, [acting, auth, pendingDisconnect])

  const confirmDisconnect = useCallback(async () => {
    if (!pendingDisconnect || acting) return
    hapticImpact('medium')
    setActing(true)
    try {
      await api.confirmConfirmation(auth, pendingDisconnect)
      hapticNotify('success')
      setPendingDisconnect(null)
      // Local identity is gone: return to the onboarding/session flow.
      onNotConnected()
    } catch (err) {
      hapticNotify('error')
      setPendingDisconnect(null)
      setFatalError(err instanceof ApiError ? err.code : 'network_error')
    } finally {
      setActing(false)
    }
  }, [acting, auth, onNotConnected, pendingDisconnect])

  const brand = (
    <Brand
      right={
        accountMasked ? (
          <span className="wallet-chip num">{accountMasked}</span>
        ) : (
          <span className="badge-testnet">Aster testnet</span>
        )
      }
    />
  )

  if (load.kind === 'loading') {
    return (
      <div className="screen">
        {brand}
        <h1 className="h1" style={{ marginTop: 4 }}>
          Аккаунт и Builder
        </h1>
        <div className="skeleton" style={{ minHeight: 148 }} aria-label="Загрузка аккаунта" />
        <div className="skeleton" style={{ minHeight: 64 }} aria-label="Загрузка проверки" />
        <div className="skeleton" style={{ minHeight: 220 }} />
      </div>
    )
  }

  if (load.kind === 'error') {
    const error = describeError(load.code)
    return (
      <div className="screen">
        {brand}
        <h1 className="h1" style={{ marginTop: 4 }}>
          Аккаунт и Builder
        </h1>
        <ErrorBox title={error.title} hint={error.hint} code={load.code}>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => {
              setLoad({ kind: 'loading' })
              void loadInitial()
            }}
          >
            Попробовать снова
          </button>
        </ErrorBox>
      </div>
    )
  }

  const { dto, transportStale } = load
  const state = stateCopy(dto.state)
  const verified = isAccountBuilderVerified(dto)
  const canReauthorize = dto.remediation === 'reauthorize'
  const reauthBusy =
    reauth.kind === 'preparing' || reauth.kind === 'signing' || reauth.kind === 'submitting'

  return (
    <div className="screen">
      {brand}
      <h1 className="h1" style={{ marginTop: 4 }}>
        Аккаунт и Builder
      </h1>

      <section className={`status-hero tone-${state.tone}`} aria-live="polite">
        <div className="status-hero-state">{state.label}</div>
        <div className="status-hero-reason">{reasonCopy(dto.reason)}</div>
        <span className={`dot ${state.tone === 'muted' ? '' : state.tone}`} aria-hidden />
      </section>

      {transportStale && (
        <div className="transport-stale" role="status">
          {TRANSPORT_STALE_WARNING}
        </div>
      )}

      <div className="kv-list" aria-label="Данные аккаунта">
        <Row label="Aster-аккаунт" value={dto.account_masked ?? '—'} />
        <Row label="Agent" value={dto.signer_masked ?? '—'} />
        {dto.identity_source === 'builder' ? (
          <>
            <Row label="Builder" value={dto.builder_name || '—'} />
            <Row label="Адрес Builder" value={dto.builder_address_masked ?? '—'} />
            <Row
              label="Комиссия Builder"
              value={dto.max_fee_rate ? formatFeeRate(dto.max_fee_rate) : '—'}
            />
          </>
        ) : (
          <Row label="Builder" value="Не используется" />
        )}
      </div>

      <div className="kv-list" aria-label="Разрешения Agent">
        <Row
          label="Perps-торговля"
          value={dto.permission.can_perp_trade ? 'Разрешена' : 'Запрещена'}
          valueClass={dto.permission.can_perp_trade ? undefined : 'tone-danger'}
        />
        <Row
          label="Вывод средств"
          value={dto.permission.can_withdraw ? 'Разрешён' : 'Запрещён'}
          valueClass={dto.permission.can_withdraw ? 'tone-danger' : undefined}
        />
        <Row
          label="Действует до"
          value={
            dto.agent_expires_at_ms ? formatDateFromMs(dto.agent_expires_at_ms) : 'Неизвестно'
          }
        />
        <Row
          label="Проверено сервером"
          value={dto.verified_at ? formatTime(dto.verified_at) : 'Нет подтверждения'}
        />
        {dto.last_check_at && <Row label="Последняя проверка" value={formatTime(dto.last_check_at)} />}
      </div>

      {fatalError && <ErrorBox title="Действие не выполнено" hint={describeError(fatalError).hint} code={fatalError} />}

      <div className="actions">
        <button
          type="button"
          className="btn btn-secondary"
          disabled={refreshing || reauthBusy || acting}
          onClick={() => void refresh()}
        >
          {refreshing ? 'Проверяем…' : 'Перепроверить разрешения'}
        </button>

        {canReauthorize && (
          <button
            type="button"
            className="btn btn-primary"
            disabled={reauthBusy || refreshing || acting}
            onClick={() => void startReauthorize()}
          >
            {reauth.kind === 'preparing'
              ? 'Готовим данные…'
              : reauth.kind === 'signing'
                ? 'Ожидает подписи…'
                : reauth.kind === 'submitting'
                  ? 'Подтверждаем…'
                  : 'Переавторизовать'}
          </button>
        )}
      </div>

      {reauth.kind === 'signing' && (
        <section className="info-box" aria-live="polite">
          <span>
            Подпишите обновление авторизации тем же кошельком. Новый Agent не создаётся, аккаунт
            останется на паузе.
          </span>
          <button
            type="button"
            className="btn btn-primary"
            disabled={reauthBusy && reauth.kind !== 'signing'}
            onClick={() => void signReauthorize()}
          >
            Подписать в кошельке
          </button>
        </section>
      )}

      {reauth.kind === 'failed' && (
        <ErrorBox
          title="Переавторизация не выполнена"
          hint={describeError(reauth.code).hint}
          code={reauth.code}
        >
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => setReauth({ kind: 'idle' })}
          >
            Закрыть
          </button>
        </ErrorBox>
      )}

      <div className="actions actions-danger">
        <button
          type="button"
          className="btn btn-danger"
          disabled={acting || reauthBusy}
          onClick={() => void startDisconnect()}
        >
          {acting && !pendingDisconnect ? 'Готовим…' : 'Отключить аккаунт'}
        </button>
      </div>

      {pendingDisconnect && (
        <Sheet
          title={DISCONNECT_CONFIRM_TITLE}
          body={verified ? `${DISCONNECT_CONFIRM_BODY}` : DISCONNECT_CONFIRM_BODY}
          onClose={() => {
            if (!acting) setPendingDisconnect(null)
          }}
        >
          <button
            type="button"
            className="btn btn-danger"
            disabled={acting}
            onClick={() => void confirmDisconnect()}
          >
            {acting ? 'Отключаем…' : 'Подтвердить отключение'}
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => setPendingDisconnect(null)}
            disabled={acting}
          >
            Отмена
          </button>
        </Sheet>
      )}
    </div>
  )
}
