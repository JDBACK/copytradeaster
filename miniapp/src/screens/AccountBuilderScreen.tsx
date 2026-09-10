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
import { MiniAppResources } from '../resources'
import { useResource, useScreenResources, useTransportStale } from '../useResource'

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
  resources: providedResources,
  foregroundSignal,
  setBackOverride,
}: {
  auth: AuthContext
  accountMasked?: string | null
  onNotConnected: () => void
  resources?: MiniAppResources
  foregroundSignal?: number
  /** Nested-shell modal stack hook: visible confirmation Sheet handles Back first. */
  setBackOverride?: (handler: (() => boolean) | null) => void
}) {
  const [refreshing, setRefreshing] = useState(false)
  const [reauth, setReauth] = useState<ReauthState>({ kind: 'idle' })
  const [pendingDisconnect, setPendingDisconnect] = useState<ConfirmationDTO | null>(null)
  const [acting, setActing] = useState(false)
  const [fatalError, setFatalError] = useState<string | null>(null)
  const [openedAt] = useState(() => Date.now())
  const [expiryReached, setExpiryReached] = useState(false)
  const adapterRef = useRef<WalletAdapter | null>(null)
  const resources = useScreenResources(providedResources, auth)
  const resource = useResource({
    resources,
    key: 'accountBuilder',
    active: true,
    strength: 'weak',
    foregroundSignal,
  })
  const resourceDto = resource.snapshot.data
  const expired = Boolean(
    resourceDto?.agent_expires_at_ms &&
      (resourceDto.agent_expires_at_ms <= openedAt ||
        resourceDto.agent_expires_at_ms <= resource.snapshot.receivedAt ||
        expiryReached),
  )
  const builderTransportUnhealthy = useTransportStale(resources, resource.snapshot.error)
  const resourceTransportStale =
    resourceDto !== null && (builderTransportUnhealthy || expired)
  const load: LoadState = resourceDto
    ? { kind: 'ready', dto: resourceDto, transportStale: resourceTransportStale }
    : resource.snapshot.error
      ? { kind: 'error', code: resource.snapshot.error }
      : { kind: 'loading' }
  const ownerMutationState = resources.mutationState('account-builder')
  const ownerActing = ownerMutationState !== null
  const ownerActive = ownerMutationState === 'active'
  const ownerUnknownReason = resources.mutationUnknownReason('account-builder')
  const ownerUnknownSince = resources.mutationUnknownSinceRevision('account-builder')

  const closeDisconnectSheetFromBack = useCallback(() => {
    if (pendingDisconnect === null) return false
    setPendingDisconnect(null)
    return true
  }, [pendingDisconnect])

  useEffect(() => {
    setBackOverride?.(pendingDisconnect ? closeDisconnectSheetFromBack : null)
    return () => setBackOverride?.(null)
  }, [closeDisconnectSheetFromBack, pendingDisconnect, setBackOverride])

  useEffect(() => {
    const expiresAt = resourceDto?.agent_expires_at_ms ?? null
    // A new attestation can extend the deadline; reset the local semantic
    // deadline watcher to that authoritative value.
    // oxlint-disable-next-line react/set-state-in-effect
    setExpiryReached(false)
    if (!expiresAt) return
    let timer: number | null = null
    const arm = () => {
      const remaining = expiresAt - Date.now()
      if (remaining <= 0) {
        setExpiryReached(true)
        return
      }
      // Browsers clamp 32-bit timeouts. Re-arm after the maximum safe delay
      // rather than overflowing a far-future Agent expiry into an immediate
      // callback. This is a one-shot semantic deadline, never polling.
      timer = window.setTimeout(arm, Math.min(remaining, 2_147_483_647))
    }
    arm()
    return () => {
      if (timer !== null) window.clearTimeout(timer)
    }
  }, [resourceDto?.agent_expires_at_ms])

  const refresh = useCallback(async () => {
    if (!resourceDto || refreshing) return
    hapticImpact('light')
    setRefreshing(true)
    try {
      if (ownerUnknownReason === 'account-disconnect') {
        const session = await api.session(auth)
        if (!session.account_connected && session.connection_state !== 'connected') {
          resources.resolveUnknownMutation('account-builder')
          onNotConnected()
          return
        }
        resources.resolveUnknownMutation('account-builder')
        return
      }
      await resource.refresh('strong')
      if (ownerUnknownReason === 'account-reauthorize') {
        resources.resolveUnknownMutation('account-builder')
      }
      resources.invalidate(['dashboard', 'status'])
    } catch (err) {
      if (resources.handleInvalidSessionError(err)) return
      if (err instanceof ApiError && err.code === 'aborted') return
      hapticNotify('error')
    } finally {
      setRefreshing(false)
    }
  }, [auth, onNotConnected, ownerUnknownReason, refreshing, resource, resourceDto, resources])

  useEffect(() => {
    if (
      ownerUnknownReason !== 'account-reauthorize' ||
      ownerUnknownSince === null
    ) return
    const snapshot = resource.snapshot
    if (
      snapshot.revision > ownerUnknownSince &&
      snapshot.data !== null &&
      snapshot.error === null &&
      !snapshot.stale &&
      snapshot.strength === 'strong'
    ) {
      resources.resolveUnknownMutation('account-builder')
    }
  }, [ownerUnknownReason, ownerUnknownSince, resource.snapshot, resources])

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
    if (!resourceDto || resourceDto.remediation !== 'reauthorize') return
    const release = resources.acquireMutation('account-builder')
    if (!release) return
    if (reauth.kind === 'preparing' || reauth.kind === 'signing' || reauth.kind === 'submitting') {
      release()
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
      if (resources.handleInvalidSessionError(err)) return
      hapticNotify('error')
      const code =
        err instanceof ApiError ? err.code : err instanceof WalletError ? err.code : 'network_error'
      if (err instanceof ApiError) reportApi('reauthorize_prepare_failed', err)
      setReauth({ kind: 'failed', code })
    } finally {
      // Only the prepare request is admitted here. Once the signing prompt is
      // ready there is no network mutation in flight; submit takes a fresh
      // owner lock below. Back/unmount during prepare therefore cannot start a
      // duplicate prepare from a newly mounted screen.
      release()
    }
  }, [auth, resourceDto, reauth.kind, reportApi, resources])

  const signReauthorize = useCallback(async () => {
    if (reauth.kind !== 'signing') return
    const release = resources.acquireMutation('account-builder')
    if (!release) return
    const { prepare } = reauth
    if (!prepare.pending_id || !prepare.eip712) {
      release()
      return
    }
    hapticImpact('medium')
    setReauth({ kind: 'submitting' })
    try {
      const wallet = await resolveAdapter()
      const address = await wallet.connect()
      const signature = await wallet.signTypedData(address, prepare.eip712)
      await api.reauthorizeSubmit(auth, prepare.pending_id, signature)
      // The proof was rebuilt server-side: a strong owner read is shared with
      // equivalent post-write checks. Fence any read that began before submit,
      // then wait for the current invalidation revision (max one follow-up).
      resources.invalidate(['accountBuilder', 'dashboard', 'status'])
      try {
        await resources.refreshCurrent('accountBuilder', 'strong')
      } catch (verifyErr) {
        if (resources.handleInvalidSessionError(verifyErr)) return
        // The submit returned success, but until a later authoritative
        // attestation lands the old remediation must not enable another write.
        resources.markMutationUnknown('account-builder', 'account-reauthorize')
        setReauth({ kind: 'failed', code: verifyErr instanceof ApiError ? verifyErr.code : 'network_error' })
        return
      }
      hapticNotify('success')
      setReauth({ kind: 'idle' })
    } catch (err) {
      if (resources.handleInvalidSessionError(err)) return
      const code =
        err instanceof ApiError ? err.code : err instanceof WalletError ? err.code : 'network_error'
      if (code === 'network_error' || code === 'timeout_error') {
        try {
          // The submit may have committed. A strong attestation that began
          // before submit is causally too old to reconcile that outcome, so
          // fence it and let refreshCurrent perform at most one post-boundary
          // follow-up before deciding whether the authorization is verified.
          resources.invalidate(['accountBuilder'])
          const current = await resources.refreshCurrent('accountBuilder', 'strong')
          if (isAccountBuilderVerified(current)) {
            resources.invalidate(['dashboard', 'status'])
            hapticNotify('success')
            setReauth({ kind: 'idle' })
            return
          }
        } catch (verifyErr) {
          if (resources.handleInvalidSessionError(verifyErr)) return
          // Mutation outcome + attestation are both unknown. Retain typed
          // uncertainty across Back/remount; a later strong attestation may clear it.
          resources.markMutationUnknown('account-builder', 'account-reauthorize')
        }
      }
      hapticNotify('error')
      if (err instanceof ApiError) reportApi('reauthorize_submit_failed', err)
      setReauth({ kind: 'failed', code })
    } finally {
      release()
    }
  }, [auth, reauth, reportApi, resources])

  const startDisconnect = useCallback(async () => {
    if (acting || pendingDisconnect) return
    const release = resources.acquireMutation('account-builder')
    if (!release) return
    hapticImpact('medium')
    setActing(true)
    setFatalError(null)
    try {
      const pending = await api.beginConfirmation(auth, 'disconnect')
      setPendingDisconnect(pending)
    } catch (err) {
      if (resources.handleInvalidSessionError(err)) return
      hapticNotify('error')
      setFatalError(err instanceof ApiError ? err.code : 'network_error')
    } finally {
      setActing(false)
      // beginConfirmation is admission-only; the returned token is local UI
      // state and confirm acquires a new owner lock. A Back/unmount while this
      // request is pending cannot issue a second begin from a remount.
      release()
    }
  }, [acting, auth, pendingDisconnect, resources])

  const confirmDisconnect = useCallback(async () => {
    if (!pendingDisconnect || acting || ownerActing) return
    const release = resources.acquireMutation('account-builder')
    if (!release) return
    hapticImpact('medium')
    setActing(true)
    try {
      await api.confirmConfirmation(auth, pendingDisconnect)
      hapticNotify('success')
      setPendingDisconnect(null)
      // Local identity is gone: return to the onboarding/session flow.
      onNotConnected()
    } catch (err) {
      setPendingDisconnect(null)
      if (resources.handleInvalidSessionError(err)) return
      const code = err instanceof ApiError ? err.code : 'network_error'
      if (code === 'network_error' || code === 'timeout_error') {
        try {
          const session = await api.session(auth)
          if (!session.account_connected && session.connection_state !== 'connected') {
            hapticNotify('success')
            onNotConnected()
            return
          }
        } catch (verifyErr) {
          if (resources.handleInvalidSessionError(verifyErr)) return
          // Disconnect may already have committed and consumed the token. Keep
          // typed uncertainty until a later authoritative session check.
          resources.markMutationUnknown('account-builder', 'account-disconnect')
        }
      }
      hapticNotify('error')
      setFatalError(code)
    } finally {
      setActing(false)
      release()
    }
  }, [acting, auth, onNotConnected, ownerActing, pendingDisconnect, resources])

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
          Подключение Aster
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
          Подключение Aster
        </h1>
        <ErrorBox title={error.title} hint={error.hint} code={load.code}>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => {
              void resource.refresh('weak').catch(() => {})
            }}
          >
            Попробовать снова
          </button>
        </ErrorBox>
      </div>
    )
  }

  const { dto, transportStale } = load
  // P2.5 pattern: past data stays visible, but a transport failure strips
  // the green verified visual until a refresh succeeds again.  A
  // transport-stale snapshot must never render as fresh verified.
  const hero = transportStale ? { label: 'Данные устарели', tone: 'warning' as const } : stateCopy(dto.state)
  const verified = isAccountBuilderVerified(dto) && !transportStale
  const canReauthorize = dto.remediation === 'reauthorize'
  const reauthBusy =
    reauth.kind === 'preparing' || reauth.kind === 'signing' || reauth.kind === 'submitting'

  return (
    <div className="screen">
      {brand}
      <h1 className="h1" style={{ marginTop: 4 }}>
        Подключение Aster
      </h1>

      <section className={`status-hero tone-${hero.tone}`} aria-live="polite">
        <div className="status-hero-state">{hero.label}</div>
        <div className="status-hero-reason">{reasonCopy(dto.reason)}</div>
        <span className={`dot ${hero.tone === 'muted' ? '' : hero.tone}`} aria-hidden />
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
          label="Торговля фьючерсами"
          value={dto.permission.can_perp_trade == null ? 'Не удалось проверить' : dto.permission.can_perp_trade ? 'Разрешена' : 'Запрещена'}
          valueClass={dto.permission.can_perp_trade === false ? 'tone-danger' : undefined}
        />
        <Row
          label="Вывод средств"
          value={dto.permission.can_withdraw == null ? 'Не удалось проверить' : dto.permission.can_withdraw ? 'Разрешён' : 'Запрещён'}
          valueClass={dto.permission.can_withdraw ? 'tone-danger' : undefined}
        />
        <Row
          label="Действует до"
          value={
            dto.agent_expires_at_ms ? formatDateFromMs(dto.agent_expires_at_ms) : 'Неизвестно'
          }
        />
        <Row
          label="Подтверждение Builder"
          value={
            dto.verified_at
              ? formatTime(dto.verified_at)
              : // A direct connection has nothing to confirm; saying "no
                // confirmation" next to "Builder не используется" reads like a
                // problem where the honest answer is that no Builder is needed.
                dto.state === 'not_applicable'
                ? 'Не требуется'
                : 'Нет подтверждения'
          }
        />
        {dto.last_check_at && <Row label="Последняя проверка" value={formatTime(dto.last_check_at)} />}
      </div>

      {fatalError && <ErrorBox title="Действие не выполнено" hint={describeError(fatalError).hint} code={fatalError} />}

      <div className="actions">
        <button
          type="button"
          className="btn btn-secondary"
          disabled={refreshing || reauthBusy || acting || ownerActive}
          onClick={() => void refresh()}
        >
          {refreshing ? 'Проверяем…' : 'Перепроверить разрешения'}
        </button>

        {canReauthorize && (
          <button
            type="button"
            className="btn btn-primary"
            disabled={reauthBusy || refreshing || acting || ownerActing}
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
          disabled={acting || reauthBusy || ownerActing}
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
            disabled={acting || ownerActing}
            onClick={() => void confirmDisconnect()}
          >
            {acting || ownerActing ? 'Отключаем…' : 'Подтвердить отключение'}
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
