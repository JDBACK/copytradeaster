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
import { describeError } from '../errors'
import {
  RISK_DEMO_BALANCE_USDT,
  demoLossUsdt,
  enumerateRiskSteps,
  formatRiskPercent,
  isRiskAllowed,
  isRiskDirty,
  nearestRiskStep,
} from '../riskView'
import type { RiskDTO } from '../types'
import { createToggleGate, type ToggleGate } from '../notificationsView'
import { hapticNotify } from '../telegram'

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; dto: RiskDTO }
  | { kind: 'error'; code: string }

export function RiskScreen({
  auth,
  accountMasked,
  onSaved,
}: {
  auth: AuthContext
  accountMasked?: string | null
  /** Fired after a successful save so the shell can stale dependent tabs. */
  onSaved?: () => void
}) {
  const [load, setLoad] = useState<LoadState>({ kind: 'loading' })
  // Last server-confirmed fraction; the only save truth.
  const [confirmed, setConfirmed] = useState<string | null>(null)
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
  // Synchronous double-tap lock: state.saving exists only after re-render.
  const gate = useRef<ToggleGate | null>(null)
  if (gate.current === null) gate.current = createToggleGate()

  const loadInitial = useCallback(async () => {
    setLoad({ kind: 'loading' })
    try {
      const dto = await api.risk(auth)
      // Legacy guard: a stored value may predate the current grid (policy
      // change).  Never silently rewrite it — keep it as the confirmed truth
      // and pre-select the nearest grid step, so the user explicitly picks
      // and saves.  The banner below explains the situation.
      const grid = enumerateRiskSteps(dto)
      const selected =
        grid.length > 0 && !isRiskAllowed(dto, dto.risk_percent)
          ? (nearestRiskStep(dto, dto.risk_percent) ?? dto.risk_percent)
          : dto.risk_percent
      setConfirmed(dto.risk_percent)
      setSelected(selected)
      selectedRef.current = selected
      setSaveError(null)
      setLoad({ kind: 'ready', dto })
    } catch (err) {
      setLoad({ kind: 'error', code: err instanceof ApiError ? err.code : 'network_error' })
    }
  }, [auth])

  useEffect(() => {
    // Initial fetch only: loadInitial awaits before its first setState.
    // oxlint-disable-next-line react/set-state-in-effect
    void loadInitial()
  }, [loadInitial])

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
          <button type="button" className="btn btn-secondary" onClick={() => void loadInitial()}>
            Повторить
          </button>
        </div>
      </div>
    )
  }

  const dto = load.dto
  const current = confirmed ?? dto.risk_percent
  const active = selected ?? dto.risk_percent
  const steps = enumerateRiskSteps(dto)
  // Stored value off the current grid (legacy policy): no tick is the truth,
  // the ruler pre-selects the nearest step, and the banner asks for an
  // explicit save.  Proven empty on the live DB (all rows on-grid) — this is
  // pure defence for future policy changes.
  const legacy = steps.length > 0 && !isRiskAllowed(dto, current)
  const dirty = confirmed !== null && selected !== null && isRiskDirty(current, active)
  const allowed = steps.includes(active) && isRiskAllowed(dto, active)
  const canSave = dirty && allowed && !saving

  const save = async () => {
    // Read the freshest draft synchronously: a gesture may have updated the
    // ref in the same tick whose state update has not been flushed yet.
    const fraction = selectedRef.current ?? selected
    if (fraction === null || !isRiskAllowed(dto, fraction)) return
    // Gate first: two taps in the same tick must admit exactly one mutation.
    if (!gate.current?.tryBegin()) return
    setSaving(true)
    setSaveError(null)
    try {
      const next = await api.setRiskPercent(auth, fraction)
      setConfirmed(next.risk_percent)
      setSelected(next.risk_percent)
      selectedRef.current = next.risk_percent
      setLoad({ kind: 'ready', dto: next })
      flashToast(`Сохранено · ${formatRiskPercent(next.risk_percent)}`)
      hapticNotify('success')
      onSaved?.()
    } catch (err) {
      setSaveError(err instanceof ApiError ? err.code : 'network_error')
      hapticNotify('error')
    } finally {
      setSaving(false)
      gate.current?.end()
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

      {legacy && (
        <div className="info-box risk-legacy-note">
          <span>
            Сохранено {formatRiskPercent(current)} — такое значение больше недоступно. Выберите
            ближайшее на шкале и нажмите Сохранить.
          </span>
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
              <button type="button" className="btn btn-secondary" onClick={() => void loadInitial()}>
                Повторить
              </button>
            </div>
          </div>
        ) : (
          <RiskRuler steps={steps} active={active} onPick={pick} disabled={saving} />
        )}
        <div className="risk-dirty-line num">
          <span>Сейчас {formatRiskPercent(current)}</span>
          {dirty && <span>После {formatRiskPercent(active)}</span>}
        </div>
      </section>

      <section className="panel risk-demo">
        <div className="risk-demo-head">
          <span>Что это значит</span>
          <span className="risk-demo-badge">Демо-расчёт</span>
        </div>
        <div className="risk-demo-body">
          <span className="risk-demo-icon" aria-hidden>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
              <rect x="5" y="3.5" width="14" height="17" rx="2.5" stroke="currentColor" strokeWidth="1.6" />
              <path d="M8.5 7.5h7M8.5 12h.01M12 12h.01M15.5 12h.01M8.5 15.5h.01M12 15.5h.01M15.5 15.5h.01" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
            </svg>
          </span>
          <span>
            При балансе {RISK_DEMO_BALANCE_USDT.toLocaleString('ru-RU')} USDT торговое ядро рассчитает
            позицию так, чтобы убыток до Stop был около {demoLossUsdt(RISK_DEMO_BALANCE_USDT, active)}.
          </span>
        </div>
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
          aria-busy={saving}
          onClick={() => void save()}
        >
          {saving ? 'Сохранение…' : `Сохранить ${formatRiskPercent(active)}`}
        </button>
      </div>

      <span data-risk-saving={String(saving)} hidden />

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
