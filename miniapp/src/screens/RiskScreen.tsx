/** Risk on Stop screen (nested, P2.4, snap-strip redesign).
 *
 * Single numeric setting backed by the backend-owned policy: the scroll
 * picker positions are enumerated from the server DTO (min/max/step), so a
 * position is valid by construction and there is no manual input.  The UI
 * works in percents, the API travels in fraction Decimals.  The confirmed
 * value on screen only ever changes on a server response; failures keep
 * the last confirmed state for retry. */

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, type AuthContext } from '../api'
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
import { hapticImpact, hapticNotify } from '../telegram'

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; dto: RiskDTO }
  | { kind: 'error'; code: string }

/** Width of one ruler cell in px; must match `.risk-tick-btn` in CSS. */
const RULER_CELL_PX = 64

/** Exact edge padding so the first/last cell centers precisely under the
 *  fixed cursor.  Percentage CSS padding is unreliable here: it resolves
 *  against the wrapper while centering needs half of the ruler's own
 *  (bleed-widened) width — and WebKit does not re-snap programmatic
 *  scrolls the way Chromium does.  clientWidth is immune to all of that
 *  (padding never changes it), so compute in px on mount and on resize. */
function fitRulerPadding(strip: HTMLElement): void {
  const pad = Math.max(0, strip.clientWidth / 2 - RULER_CELL_PX / 2)
  strip.style.paddingLeft = `${pad}px`
  strip.style.paddingRight = `${pad}px`
}

/** Paint the fish-eye: per-tick `--off` (0 at the strip center → 1 at the
 *  edge) drives tick scale/label fade in CSS.  Returns the centered
 *  fraction.  Direct DOM writes under rAF — no React state per frame. */
function paintRuler(strip: HTMLElement): string | null {
  const stripRect = strip.getBoundingClientRect()
  const mid = stripRect.left + stripRect.width / 2
  const half = Math.max(stripRect.width / 2, 1)
  let best: string | null = null
  let bestDist = Number.POSITIVE_INFINITY
  for (const node of strip.querySelectorAll('[data-fraction]')) {
    const el = node as HTMLElement
    const rect = el.getBoundingClientRect()
    const dist = Math.abs(rect.left + rect.width / 2 - mid)
    el.style.setProperty('--off', Math.min(1, dist / half).toFixed(3))
    if (dist < bestDist) {
      bestDist = dist
      best = el.dataset.fraction ?? null
    }
  }
  return best
}

/** Horizontal snap ruler: swipe / mouse drag / wheel / arrow keys.
 *  Pure: positions and the active value come from props, no fetching.
 *  One cell per policy step — a tick plus its label; the big value above
 *  follows the selection live while "Сейчас" keeps the confirmed truth. */
export function RiskRuler({
  steps,
  active,
  onPick,
  disabled = false,
}: {
  steps: string[]
  active: string
  onPick: (fraction: string, opts?: { silent?: boolean }) => void
  /** Frozen while a save is in flight: no re-pick can land mid-POST and get
   *  overwritten by the server response. */
  disabled?: boolean
}) {
  const stripRef = useRef<HTMLDivElement | null>(null)
  const rafId = useRef<number | null>(null)
  const trailTimer = useRef<number | null>(null)
  const coolTimer = useRef<number | null>(null)
  // True briefly after an explicit tap/keyboard pick so the following
  // programmatic jump never re-picks mid-flight.
  const cooling = useRef(false)
  // Last value acknowledged with a landing haptic (tap or scroll settle).
  const vibrated = useRef(active)
  const drag = useRef<{ startX: number; startLeft: number; moved: boolean; pointerId: number } | null>(
    null,
  )
  const suppressClick = useRef(false)
  // Latest freeze flag for async scroll/settle callbacks scheduled before
  // the lock: they must re-check it instead of applying a stale pick mid-POST.
  const disabledRef = useRef(disabled)
  // Previous freeze state: leaving the lock re-centers the active tick.
  const wasDisabled = useRef(disabled)

  const centerFraction = useCallback((fraction: string) => {
    const strip = stripRef.current
    if (!strip) return
    const jump = (): number | null => {
      const el = strip.querySelector(`[data-fraction="${CSS.escape(fraction)}"]`) as HTMLElement | null
      if (!el) return null
      // Exact jump (not scrollIntoView smooth: smooth + mandatory snap stalls
      // on multi-cell jumps). Selection state already updated synchronously,
      // so the ruler must follow instantly, not chase behind.
      const stripRect = strip.getBoundingClientRect()
      const elRect = el.getBoundingClientRect()
      const delta = elRect.left + elRect.width / 2 - (stripRect.left + stripRect.width / 2)
      strip.scrollTo({ left: strip.scrollLeft + delta, behavior: 'auto' })
      return delta
    }
    jump()
    // One correction pass: engines that re-snap programmatic scrolls
    // (WebKit) can leave a residual offset — measure and finish it.
    requestAnimationFrame(() => {
      if (stripRef.current !== strip) return
      const residual = jump()
      if (residual !== null && Math.abs(residual) > 1) paintRuler(strip)
    })
  }, [])

  // Freeze lifecycle: entering disabled cancels pending scroll/settle work,
  // and leaving it re-centers the active tick — a native touch scroll may
  // have visually slipped the strip mid-POST while selection stayed frozen
  // (handlers ignore it, but the container is still a live scroll box until
  // CSS locks it — belt and braces).
  useEffect(() => {
    disabledRef.current = disabled
    if (disabled) {
      if (rafId.current !== null) {
        cancelAnimationFrame(rafId.current)
        rafId.current = null
      }
      if (trailTimer.current !== null) {
        window.clearTimeout(trailTimer.current)
        trailTimer.current = null
      }
    } else if (wasDisabled.current) {
      centerFraction(active)
      const strip = stripRef.current
      if (strip) paintRuler(strip)
    }
    wasDisabled.current = disabled
  }, [disabled, active, centerFraction])

  // Live tracking: every scroll frame repaints the fish-eye and moves the
  // selection silently, so the big value follows the finger with no lag.
  // A trailing settle then acknowledges the landing value with one haptic.
  const handleScroll = useCallback(() => {
    if (disabled || cooling.current) return
    if (rafId.current !== null) cancelAnimationFrame(rafId.current)
    rafId.current = requestAnimationFrame(() => {
      rafId.current = null
      const strip = stripRef.current
      if (!strip || disabledRef.current || cooling.current) return
      const best = paintRuler(strip)
      const rendered = strip.querySelector('[aria-checked="true"]')?.getAttribute('data-fraction')
      if (best !== null && best !== rendered) onPick(best, { silent: true })
    })
    if (trailTimer.current !== null) window.clearTimeout(trailTimer.current)
    trailTimer.current = window.setTimeout(() => {
      const strip = stripRef.current
      if (!strip || disabledRef.current) return
      const best = paintRuler(strip)
      if (best !== null && best !== vibrated.current) {
        vibrated.current = best
        onPick(best)
      }
    }, 90)
  }, [disabled, onPick])

  useEffect(
    () => () => {
      if (rafId.current !== null) cancelAnimationFrame(rafId.current)
      if (trailTimer.current !== null) window.clearTimeout(trailTimer.current)
      if (coolTimer.current !== null) window.clearTimeout(coolTimer.current)
    },
    [],
  )

  // Center the confirmed position on mount and paint the first frame.
  // Padding is fitted first (px-exact, see fitRulerPadding) and refitted
  // on resize (rotation), keeping the checked tick under the cursor.
  useEffect(() => {
    const strip = stripRef.current
    if (!strip) return
    fitRulerPadding(strip)
    const checked = strip.querySelector('[aria-checked="true"]')?.getAttribute('data-fraction')
    if (checked) centerFraction(checked)
    paintRuler(strip)
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => {
      fitRulerPadding(strip)
      const current = strip.querySelector('[aria-checked="true"]')?.getAttribute('data-fraction')
      if (current) centerFraction(current)
      else paintRuler(strip)
    })
    observer.observe(strip)
    return () => observer.disconnect()
  }, [centerFraction])

  // Desktop wheel: vertical wheel scrolls the strip horizontally.
  useEffect(() => {
    const strip = stripRef.current
    if (!strip || disabled) return
    const onWheel = (event: WheelEvent) => {
      if (Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return
      if (strip.scrollWidth <= strip.clientWidth + 1) return
      event.preventDefault()
      strip.scrollLeft += event.deltaY
    }
    strip.addEventListener('wheel', onWheel, { passive: false })
    return () => strip.removeEventListener('wheel', onWheel)
  }, [disabled])

  const tap = (fraction: string) => {
    if (disabled) return
    cooling.current = true
    if (coolTimer.current !== null) window.clearTimeout(coolTimer.current)
    coolTimer.current = window.setTimeout(() => {
      cooling.current = false
    }, 150)
    vibrated.current = fraction
    onPick(fraction)
    centerFraction(fraction)
    // WebKit does not move focus to buttons on mouse/tap click: without
    // this, arrow keys stay dead until the next Tab.
    const strip = stripRef.current
    const el = strip?.querySelector(`[data-fraction="${CSS.escape(fraction)}"]`) as HTMLElement | null
    el?.focus({ preventScroll: true })
  }

  const onPointerDown = (event: React.PointerEvent) => {
    if (disabled || event.pointerType !== 'mouse' || event.button !== 0) return
    const strip = stripRef.current
    if (!strip) return
    drag.current = { startX: event.clientX, startLeft: strip.scrollLeft, moved: false, pointerId: event.pointerId }
    strip.classList.add('dragging')
  }

  const onPointerMove = (event: React.PointerEvent) => {
    const state = drag.current
    const strip = stripRef.current
    if (!state || !strip || event.pointerId !== state.pointerId) return
    const dx = event.clientX - state.startX
    if (Math.abs(dx) > 6) state.moved = true
    if (state.moved) strip.scrollLeft = state.startLeft - dx
  }

  const endDrag = (event: React.PointerEvent) => {
    const state = drag.current
    if (!state || event.pointerId !== state.pointerId) return
    drag.current = null
    stripRef.current?.classList.remove('dragging')
    if (state.moved) {
      // Swallow the click that follows a drag so a swipe never selects.
      suppressClick.current = true
      handleScroll()
    }
  }

  const onClickCapture = (event: React.MouseEvent) => {
    if (suppressClick.current) {
      suppressClick.current = false
      event.stopPropagation()
      event.preventDefault()
    }
  }

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (disabled) return
    const index = steps.indexOf(active)
    let next = -1
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = Math.min(steps.length - 1, index + 1)
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = Math.max(0, index - 1)
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = steps.length - 1
    else return
    event.preventDefault()
    tap(steps[next])
    const strip = stripRef.current
    const el = strip?.querySelector(`[data-fraction="${CSS.escape(steps[next])}"]`) as HTMLElement | null
    el?.focus({ preventScroll: true })
  }

  return (
    <div className="risk-ruler-wrap">
      <div
        ref={stripRef}
        className="risk-ruler"
        role="radiogroup"
        aria-label="Выбор риска"
        aria-disabled={disabled || undefined}
        onScroll={handleScroll}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onClickCapture={onClickCapture}
        onKeyDown={onKeyDown}
      >
        {steps.map((step) => (
          <button
            key={step}
            type="button"
            role="radio"
            aria-checked={step === active}
            aria-label={formatRiskPercent(step)}
            data-fraction={step}
            tabIndex={step === active ? 0 : -1}
            disabled={disabled}
            className={`risk-tick-btn num${step === active ? ' active' : ''}`}
            onClick={() => tap(step)}
          >
            <span className="risk-tick-mark" aria-hidden />
            <span className="risk-tick-label" aria-hidden>
              {formatRiskPercent(step)}
            </span>
          </button>
        ))}
      </div>
      <div className="risk-ruler-hint tone-muted">Тяните шкалу, чтобы выбрать значение</div>
      <div className="risk-ruler-cursor" aria-hidden />
    </div>
  )
}

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
  // Silent scroll tracking never vibrates; taps and landing settles do.
  const pick = useCallback((fraction: string, opts?: { silent?: boolean }) => {
    if (opts?.silent !== true) hapticImpact('light')
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
    if (selected === null || !isRiskAllowed(dto, selected)) return
    // Gate first: two taps in the same tick must admit exactly one mutation.
    if (!gate.current?.tryBegin()) return
    setSaving(true)
    setSaveError(null)
    try {
      const next = await api.setRiskPercent(auth, selected)
      setConfirmed(next.risk_percent)
      setSelected(next.risk_percent)
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
