/** Shared presentational pieces (part-1 scope). */

import type { ReactNode } from 'react'

export function Brand({ right }: { right?: ReactNode }) {
  return (
    <header className="brand">
      <div className="brand-mark" aria-hidden />
      <div className="brand-name">
        ASTER <span>COPY</span>
      </div>
      {right}
    </header>
  )
}

const RAIL_STEPS = ['Wallet', 'Agent', 'Builder', 'Sync'] as const

export function TrustRail({
  active,
  doneCount,
}: {
  active: number
  doneCount: number
}) {
  return (
    <nav className="rail" aria-label="Этапы подключения">
      {RAIL_STEPS.map((label, index) => {
        const state = index < doneCount ? 'done' : index === active ? 'active' : ''
        return (
          <div key={label} className={`rail-step ${state}`}>
            <div className="rail-circle">
              {index < doneCount ? (
                <svg width="18" height="18" viewBox="0 0 20 20" fill="none" aria-hidden>
                  <path
                    d="M4 10.5 8.2 14.5 16 6"
                    stroke="currentColor"
                    strokeWidth="2.2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              ) : (
                <RailGlyph index={index} />
              )}
            </div>
            <div className="rail-label">{label}</div>
          </div>
        )
      })}
    </nav>
  )
}

function RailGlyph({ index }: { index: number }) {
  const common = { stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round' as const, fill: 'none' }
  switch (index) {
    case 0:
      return (
        <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden>
          <rect x="3" y="6" width="18" height="13" rx="3" {...common} />
          <path d="M3 10h18M16.5 15h1.5" {...common} />
        </svg>
      )
    case 1:
      return (
        <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden>
          <circle cx="12" cy="8" r="3.4" {...common} />
          <path d="M5 19c1.4-3.2 4-4.8 7-4.8s5.6 1.6 7 4.8" {...common} />
        </svg>
      )
    case 2:
      return (
        <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden>
          <path d="M12 3.5 19 6v5.2c0 4.6-2.9 7.6-7 9.3-4.1-1.7-7-4.7-7-9.3V6l7-2.5Z" {...common} />
          <path d="m9 11.8 2.1 2.1L15.4 9.6" {...common} />
        </svg>
      )
    default:
      return (
        <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden>
          <path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3" {...common} />
          <path d="M19.8 3.8v3.4h-3.4" {...common} />
        </svg>
      )
  }
}

export function ErrorBox({ title, hint, children }: { title: string; hint: string; children?: ReactNode }) {
  return (
    <section className="error-box" role="alert">
      <div className="error-title">{title}</div>
      <div className="error-hint">{hint}</div>
      {children}
    </section>
  )
}

export function Sheet({ title, body, children }: { title: string; body: string; children: ReactNode }) {
  return (
    <div className="overlay" role="dialog" aria-modal aria-label={title}>
      <div className="sheet">
        <div className="h2">{title}</div>
        <p className="lede">{body}</p>
        {children}
      </div>
    </div>
  )
}

export function Row({ label, value, valueClass }: { label: ReactNode; value: ReactNode; valueClass?: string }) {
  return (
    <div className="row">
      <div className="row-label">{label}</div>
      <div className={`row-value num ${valueClass ?? ''}`}>{value}</div>
    </div>
  )
}

export function Check({ ok }: { ok: boolean }) {
  return (
    <svg width="17" height="17" viewBox="0 0 20 20" fill="none" aria-hidden>
      <circle cx="10" cy="10" r="8.4" stroke={ok ? 'var(--success)' : 'var(--danger)'} strokeWidth="1.7" />
      {ok ? (
        <path d="m6.4 10.3 2.4 2.4 4.8-5" stroke="var(--success)" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
      ) : (
        <path d="m7 7 6 6m0-6-6 6" stroke="var(--danger)" strokeWidth="1.9" strokeLinecap="round" />
      )}
    </svg>
  )
}
