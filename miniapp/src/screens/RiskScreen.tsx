/** Risk on Stop screen (nested, P2.4, snap-strip redesign).
 *
 * Single numeric setting backed by the backend-owned policy: the scroll
 * picker positions are enumerated from the server DTO (min/max/step), so a
 * position is valid by construction and there is no manual input.  The UI
 * works in percents, the API travels in fraction Decimals.  The confirmed
 * value on screen only ever changes on a server response; failures keep
 * the last confirmed state for retry.
 *
 * The ruler control itself lives in components/RiskRuler.tsx (S2.5/S3.4):
 * it owns gesture/animation lifecycle; this screen owns draft/confirmed/save. */

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, type AuthContext } from '../api'
import { RiskRuler } from '../components/RiskRuler'
import { Brand, ErrorBox } from '../components/ui'
import { formatUsdt, formatDateTime } from '../format'
import { describeError } from '../errors'
import {
  enumerateRiskSteps,
  formatRiskPercent,
  isRiskAllowed,
  isRiskDirty,
  nearestRiskStep,
} from '../riskView'
import type { RiskDTO } from '../types'
import { hapticNotify } from '../telegram'
import { MiniAppResources } from '../resources'
import { useResource, useScreenResources } from '../useResource'

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; dto: RiskDTO }
  | { kind: 'error'; code: string }

export function RiskScreen({
  auth,
  accountMasked,
  onSaved,
  onDirtyChange,
  resources: providedResources,
  foregroundSignal,
}: {
  auth: AuthContext
  accountMasked?: string | null
  /** Fired after a successful save so the shell can stale dependent tabs. */
  onSaved?: () => void
  onDirtyChange?: (dirty: boolean) => void
  resources?: MiniAppResources
  foregroundSignal?: number
}) {
  // Currently selected fraction (a strip position by construction).
  const [selected, setSelected] = useState<string | null>(null)
  // Synchronous mirror of `selected`: save reads the draft from here, not
  // from possibly unflushed React state, so the button label and the POSTed
  // Decimal fraction always match even when a gesture ends in the same tick
  // as the save tap.
  const selectedRef = useRef<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  // Success toast text ("Сохранено · 4,0%"); null hides it.
  const [toast, setToast] = useState<string | null>(null)
  const toastTimer = useRef<number | null>(null)
  const resources = useScreenResources(providedResources, auth)
  const ownerMutationState = resources.mutationState('risk')
  const ownerUnknownSince = resources.mutationUnknownSinceRevision('risk')
  const resource = useResource({
    resources,
    key: 'risk',
    active: true,
    // A retained ambiguous mutation is recovered only by post-boundary
    // authoritative evidence. Risk has one read endpoint, but marking this
    // owner read strong keeps the reconciliation contract typed and prevents a
    // stale pre-mutation weak flight from clearing the lock.
    strength: ownerMutationState === 'unknown' ? 'strong' : 'weak',
    foregroundSignal,
  })
  const preview = useResource({ resources, key: 'riskPreview', active: true, foregroundSignal })
  const resourceDto = resource.snapshot.data
  const lastServerValueRef = useRef<string | null>(null)
  const ownerSaving = ownerMutationState !== null
  const busy = saving || ownerSaving
  const transportStale =
    resourceDto !== null && (resource.snapshot.stale || resource.snapshot.error !== null)
  const load: LoadState = resourceDto
    ? { kind: 'ready', dto: resourceDto }
    : resource.snapshot.error
      ? { kind: 'error', code: resource.snapshot.error }
      : { kind: 'loading' }
  const leaveDirty = resourceDto
    ? isRiskDirty(resourceDto.risk_percent, selected ?? initialRiskSelection(resourceDto))
    : false

  useEffect(() => {
    onDirtyChange?.(leaveDirty && !busy)
    return () => onDirtyChange?.(false)
  }, [busy, leaveDirty, onDirtyChange])

  useEffect(() => {
    if (!resourceDto) return
    const previousServer = lastServerValueRef.current
    const currentDraft = selectedRef.current
    const draftWasClean = currentDraft === null || (previousServer !== null && currentDraft === previousServer)
    lastServerValueRef.current = resourceDto.risk_percent
    if (!draftWasClean) return
    const next = initialRiskSelection(resourceDto)
    selectedRef.current = next
    // External server updates replace only a clean draft. A dirty draft keeps
    // the user's choice and is revalidated against the new policy below.
    // oxlint-disable-next-line react/set-state-in-effect
    setSelected(next)
    setSaveError(null)
  }, [resourceDto])

  useEffect(() => {
    if (ownerMutationState !== 'unknown' || ownerUnknownSince === null) return
    const snapshot = resource.snapshot
    if (
      snapshot.revision > ownerUnknownSince &&
      snapshot.data !== null &&
      snapshot.error === null &&
      !snapshot.stale &&
      snapshot.strength === 'strong'
    ) {
      resources.resolveUnknownMutation('risk')
      // We no longer know whether the timed-out write committed. Conservatively
      // stale every dependent projection after authoritative Risk truth arrives.
      resources.invalidate(['dashboard', 'positions', 'status'])
      onSaved?.()
    }
  }, [onSaved, ownerMutationState, ownerUnknownSince, resource.snapshot, resources])

  useEffect(
    () => () => {
      if (toastTimer.current !== null) window.clearTimeout(toastTimer.current)
    },
    [],
  )

  const flashToast = (text: string) => {
    setToast(text)
    if (toastTimer.current !== null) window.clearTimeout(toastTimer.current)
    toastTimer.current = window.setTimeout(() => setToast(null), 2200)
  }

  // Stable identity: the ruler's scroll callbacks close over it.
  // Haptics live inside the ruler (step crossings, taps); this callback
  // only moves the draft — never the server truth.
  const pick = useCallback((fraction: string, opts?: { silent?: boolean }) => {
    void opts
    selectedRef.current = fraction
    setSelected(fraction)
    setSaveError(null)
  }, [])

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
          Риск на Stop
        </h1>
        <div className="skeleton" style={{ minHeight: 132 }} role="status" aria-label="Загрузка настроек риска" />
        <div className="skeleton" style={{ minHeight: 64 }} />
      </div>
    )
  }

  if (load.kind === 'error') {
    const copy = describeError(load.code)
    return (
      <div className="screen">
        {brand}
        <h1 className="h1" style={{ marginTop: 4 }}>
          Риск на Stop
        </h1>
        <ErrorBox title={copy.title} hint={copy.hint} />
        <div className="btn-stack">
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => void resource.refresh('weak').catch(() => {})}
          >
            Повторить
          </button>
        </div>
      </div>
    )
  }

  const dto = load.dto
  const current = dto.risk_percent
  const active = selected ?? initialRiskSelection(dto)
  const steps = enumerateRiskSteps(dto)
  // Stored value off the current grid (legacy policy): no tick is the truth,
  // the ruler pre-selects the nearest step, and the banner asks for an
  // explicit save.  Proven empty on the live DB (all rows on-grid) — this is
  // pure defence for future policy changes.
  const legacy = steps.length > 0 && !isRiskAllowed(dto, current)
  const dirty = isRiskDirty(current, active)
  const allowed = steps.includes(active) && isRiskAllowed(dto, active)
  const canSave = dirty && allowed && !busy && !transportStale

  const save = async () => {
    // Read the freshest draft synchronously: a gesture may have updated the
    // ref in the same tick whose state update has not been flushed yet.
    const fraction = selectedRef.current ?? selected
    if (fraction === null || !isRiskAllowed(dto, fraction)) return
    const release = resources.acquireMutation('risk')
    if (!release) return
    setSaving(true)
    setSaveError(null)
    try {
      const next = await api.setRiskPercent(auth, fraction)
      resources.publishConfirmed('risk', next)
      resources.invalidate(['dashboard', 'positions', 'status'])
      setSelected(next.risk_percent)
      selectedRef.current = next.risk_percent
      flashToast(`Сохранено · ${formatRiskPercent(next.risk_percent)}`)
      hapticNotify('success')
      onSaved?.()
    } catch (err) {
      if (resources.handleInvalidSessionError(err)) return
      const code = err instanceof ApiError ? err.code : 'network_error'
      let verified = false
      if (code === 'network_error' || code === 'timeout_error') {
        // Fence any Risk read admitted before the ambiguous write. The first
        // await may join that old flight, but refreshCurrent performs at most
        // one post-boundary follow-up before we classify the outcome.
        resources.invalidate(['risk'])
        try {
          const currentDto = await resources.refreshCurrent('risk', 'strong')
          if (currentDto.risk_percent === fraction) {
            resources.publishConfirmed('risk', currentDto)
            resources.invalidate(['dashboard', 'positions', 'status'])
            setSelected(currentDto.risk_percent)
            selectedRef.current = currentDto.risk_percent
            flashToast(`Сохранено · ${formatRiskPercent(currentDto.risk_percent)}`)
            hapticNotify('success')
            onSaved?.()
            verified = true
          }
        } catch {
          // Both mutation outcome and reconciliation are unknown. Retain the
          // session owner lock across Back/remount; only a later authoritative
          // read (manual/reactivation/foreground) may resolve it.
          resources.markMutationUnknown('risk', 'risk')
        }
      }
      if (!verified) {
        setSaveError(code)
        hapticNotify('error')
      }
    } finally {
      setSaving(false)
      release()
    }
  }

  return (
    <div className="screen">
      {brand}
      <h1 className="h1" style={{ marginTop: 4 }}>
        Риск на Stop
      </h1>
      <div className="lede tone-muted">
        Допустимый убыток от доступного баланса, если сработает Stop
      </div>

      {saveError && (
        <ErrorBox
          title={describeError(saveError).title}
          hint="Настройка не изменилась — показано последнее подтверждённое значение."
        />
      )}

      {transportStale && (
        <div className="info-box risk-legacy-note" role="status">
          Показано последнее подтверждённое значение. Данные требуют проверки перед сохранением.
        </div>
      )}

      {legacy && (
        <div className="info-box risk-legacy-note">
          <span>
            Сохранено {formatRiskPercent(current)} — такое значение больше недоступно. Выберите
            ближайшее на шкале и нажмите Сохранить.
          </span>
        </div>
      )}

      {dirty && !allowed && !legacy && (
        <div className="info-box risk-legacy-note" role="status">
          Параметры риска изменились на сервере. Выберите доступное значение заново перед сохранением.
        </div>
      )}

      <section
        className="panel risk-current"
        aria-label={
          dirty
            ? `Новое значение: ${formatRiskPercent(active)}`
            : `Текущее значение: ${formatRiskPercent(current)}`
        }
      >
        <div className="risk-current-label">{dirty ? 'Новое значение' : 'Текущее значение'}</div>
        <div className="risk-current-value num">
          {formatRiskPercent(dirty ? active : current)}
        </div>
        {steps.length === 0 ? (
          <div className="risk-ruler-fallback">
            <ErrorBox title="Шкала недоступна" hint="Сервер вернул пустую шкалу риска. Попробуйте обновить." />
            <div className="btn-stack">
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => void resource.refresh('weak').catch(() => {})}
              >
                Повторить
              </button>
            </div>
          </div>
        ) : (
          <RiskRuler steps={steps} active={active} onPick={pick} disabled={busy} />
        )}
        <div className="risk-dirty-line num">
          <span>Сейчас {formatRiskPercent(current)}</span>
          {dirty && <span>После {formatRiskPercent(active)}</span>}
        </div>
      </section>

      <section className="panel risk-demo">
        <h2 className="h2">Что это значит</h2>
        {preview.snapshot.data?.available_usdt != null && preview.snapshot.data.steps.some(step => !isRiskDirty(step.risk_percent, active)) ? <>
          <p className="lede">При доступных {formatUsdt(preview.snapshot.data.available_usdt)} расчётный бюджет на сделку — около {formatUsdt(preview.snapshot.data.steps.find(step => !isRiskDirty(step.risk_percent, active))?.budget_usdt)}.</p>
          <p className="freshness">{preview.snapshot.data.freshness !== 'fresh' || preview.snapshot.stale || preview.snapshot.error ? 'Пример по прежнему снимку' : 'Снимок'}: {formatDateTime(preview.snapshot.data.snapshot_captured_at)}</p>
        </> : <p className="lede">Денежный пример появится после обновления баланса.</p>}
        <p className="footer-note">Это бюджет расчёта объёма, а не гарантированный предел убытка: цена исполнения стопа и доступная сумма могут измениться.</p>
        <p className="footer-note">Новое значение используется в последующих расчётах. При проверке открытой позиции её объём может быть сокращён.</p>
      </section>

      <div className="info-box risk-leverage-note">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
          <circle cx="12" cy="12" r="8.6" stroke="currentColor" strokeWidth="1.6" />
          <path d="M12 11v5M12 7.6h.01" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
        <span>Плечо назначается автоматически для каждого актива.</span>
      </div>

      <div className="btn-stack">
        <button
          type="button"
          className="btn btn-primary num"
          disabled={!canSave}
          aria-busy={busy}
          onClick={() => void save()}
        >
          {busy ? 'Сохранение…' : `Сохранить ${formatRiskPercent(active)}`}
        </button>
        {(ownerMutationState === 'unknown' || resource.snapshot.error !== null) && (
          <button
            type="button"
            className="btn btn-secondary"
            disabled={resource.snapshot.inflight}
            onClick={() =>
              void resource
                .refresh(ownerMutationState === 'unknown' ? 'strong' : 'weak')
                .catch(() => {})
            }
          >
            Обновить данные
          </button>
        )}
      </div>

      <span data-risk-saving={String(busy)} hidden />

      {toast && (
        <div className="risk-toast" role="status">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
            <circle cx="12" cy="12" r="8.6" stroke="currentColor" strokeWidth="1.8" />
            <path
              d="m8.4 12.4 2.5 2.5 4.7-5.3"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          <span className="num">{toast}</span>
        </div>
      )}
    </div>
  )
}

function initialRiskSelection(dto: RiskDTO): string {
  const grid = enumerateRiskSteps(dto)
  return grid.length > 0 && !isRiskAllowed(dto, dto.risk_percent)
    ? (nearestRiskStep(dto, dto.risk_percent) ?? dto.risk_percent)
    : dto.risk_percent
}
