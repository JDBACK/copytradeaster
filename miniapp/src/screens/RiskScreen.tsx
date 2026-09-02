/** Risk on Stop screen (nested, эталон 05-risk-settings, P2.4).
 *
 * Single numeric setting backed by the backend-owned policy: min/max/step
 * and presets always come from the server DTO — the frontend never defines
 * them.  The UI works in percents, the API travels in fraction Decimals.
 * The confirmed value on screen only ever changes on a server response;
 * failures keep the last confirmed state for retry. */

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, type AuthContext } from '../api'
import { Brand, ErrorBox } from '../components/ui'
import { describeError } from '../errors'
import {
  RISK_DEMO_BALANCE_USDT,
  demoLossUsdt,
  formatRiskPercent,
  isRiskAllowed,
  isRiskDirty,
  parseRiskInput,
} from '../riskView'
import type { RiskDTO } from '../types'
import { createToggleGate, type ToggleGate } from '../notificationsView'
import { hapticImpact, hapticNotify } from '../telegram'

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; dto: RiskDTO }
  | { kind: 'error'; code: string }

export function RiskScreen({
  auth,
  accountMasked,
}: {
  auth: AuthContext
  accountMasked?: string | null
}) {
  const [load, setLoad] = useState<LoadState>({ kind: 'loading' })
  // Last server-confirmed fraction; the only save truth.
  const [confirmed, setConfirmed] = useState<string | null>(null)
  // Currently selected fraction (preset or parsed custom input).
  const [selected, setSelected] = useState<string | null>(null)
  const [customOpen, setCustomOpen] = useState(false)
  const [customText, setCustomText] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  // Synchronous double-tap lock: state.saving exists only after re-render.
  const gate = useRef<ToggleGate | null>(null)
  if (gate.current === null) gate.current = createToggleGate()

  const loadInitial = useCallback(async () => {
    setLoad({ kind: 'loading' })
    try {
      const dto = await api.risk(auth)
      setConfirmed(dto.risk_percent)
      setSelected(dto.risk_percent)
      setCustomText('')
      setCustomOpen(false)
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
        <div className="skeleton" style={{ minHeight: 132 }} aria-label="Загрузка настроек риска" />
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
  const dirty = confirmed !== null && selected !== null && isRiskDirty(current, active)
  const allowed = isRiskAllowed(dto, active)
  const customParsed = customText.trim() ? parseRiskInput(customText) : null
  const customInvalid =
    customOpen && customText.trim() !== '' && (customParsed === null || !isRiskAllowed(dto, customParsed))
  const canSave = dirty && allowed && !saving

  const pick = (fraction: string) => {
    hapticImpact('light')
    setSelected(fraction)
    setSaveError(null)
  }

  const applyCustom = (text: string) => {
    setCustomText(text)
    setSaveError(null)
    const parsed = text.trim() ? parseRiskInput(text) : null
    if (parsed !== null && isRiskAllowed(dto, parsed)) setSelected(parsed)
  }

  const save = async () => {
    if (selected === null || !isRiskAllowed(dto, selected)) return
    // Gate first: two taps in the same tick must admit exactly one mutation.
    if (!gate.current?.tryBegin()) return
    setSaving(true)
    setSaveError(null)
    try {
      const next = await api.setRiskPercent(auth, selected)
      setConfirmed(next.risk_percent)
      setSelected(next.risk_percent)
      setCustomText('')
      setLoad({ kind: 'ready', dto: next })
      hapticNotify('success')
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

      <section className="panel risk-current" aria-label={`Текущее значение: ${formatRiskPercent(current)}`}>
        <div className="risk-current-label">Текущее значение</div>
        <div className="risk-current-value num">{formatRiskPercent(current)}</div>
        <div className="risk-scale" aria-hidden>
          {Array.from({ length: 25 }, (_, index) => (
            <span key={index} className="risk-tick" />
          ))}
        </div>
        <div className="risk-dirty-line num">
          <span>Сейчас {formatRiskPercent(current)}</span>
          {dirty && <span>После {formatRiskPercent(active)}</span>}
        </div>
      </section>

      <div className="risk-presets" role="group" aria-label="Готовые значения риска">
        {dto.presets.map((preset) => (
          <button
            key={preset}
            type="button"
            className={`risk-preset num${preset === active ? ' active' : ''}`}
            aria-pressed={preset === active}
            onClick={() => pick(preset)}
          >
            {formatRiskPercent(preset)}
          </button>
        ))}
      </div>

      <button
        type="button"
        className="risk-custom-row"
        aria-expanded={customOpen}
        onClick={() => {
          hapticImpact('light')
          setCustomOpen((open) => !open)
        }}
      >
        <span className="risk-custom-icon" aria-hidden>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
            <path
              d="m14.5 5.5 4 4L8 20l-4.7.7L4 16 14.5 5.5Z"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinejoin="round"
            />
          </svg>
        </span>
        <span className="risk-custom-label">Другое значение</span>
        <svg
          className="settings-chevron"
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          aria-hidden
        >
          <path
            d="m9.5 6.5 5.5 5.5-5.5 5.5"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>

      {customOpen && (
        <div className="panel risk-custom-panel">
          <label className="risk-custom-field-label" htmlFor="risk-custom-input">
            Своё значение, %
          </label>
          <input
            id="risk-custom-input"
            className="risk-custom-input num"
            inputMode="decimal"
            autoComplete="off"
            placeholder="Например: 1,5"
            value={customText}
            onChange={(event) => applyCustom(event.target.value)}
          />
          {customInvalid ? (
            <div className="risk-hint tone-danger" role="alert">
              Введите значение от 0,5% до 5% с шагом 0,5%.
            </div>
          ) : (
            <div className="risk-hint tone-muted">Можно через запятую или точку, знак % необязателен.</div>
          )}
        </div>
      )}

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
    </div>
  )
}
