/** Success screen: account connected, copying paused (decision #7: срок
 * действия Agent виден здесь). */

import type { BuilderOnboardedDTO } from '../types'
import { formatDateFromMs, formatFeeRate, formatUsdt } from '../format'
import { hapticImpact } from '../telegram'
import { Brand, Row } from '../components/ui'

export function SuccessScreen({
  result,
  onHome,
}: {
  result: BuilderOnboardedDTO
  onHome: () => void
}) {
  return (
    <div className="screen">
      <Brand right={<span className="badge-testnet">Aster testnet</span>} />

      <section className="panel state-card" style={{ marginTop: 8 }}>
        <div className="state-icon" aria-hidden>
          <svg width="38" height="38" viewBox="0 0 24 24" fill="none">
            <rect x="4.5" y="4.5" width="15" height="15" rx="4" stroke="var(--champagne)" strokeWidth="1.7" />
            <path
              d="m8.4 12.3 2.5 2.5 4.9-5.4"
              stroke="var(--success)"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            <circle cx="16.4" cy="16.6" r="3.4" fill="var(--canvas)" stroke="var(--success)" strokeWidth="1.4" />
            <path
              d="m14.9 16.6 1.1 1.1 2-2.2"
              stroke="var(--success)"
              strokeWidth="1.4"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </div>
        <h1 className="h1" style={{ margin: 0 }}>
          Подключено
        </h1>
        <div className="status-text tone-warning">Копирование на паузе</div>
        <div className="freshness">Новые входы не открываются, пока вы их не включите</div>
      </section>

      <section className="panel">
        <Row label="Кошелёк" value={result.account_masked} />
        <Row label="Agent" value={result.signer_masked} />
        <Row label="Builder" value={result.builder_name} />
        <Row label="Комиссия Builder" value={formatFeeRate(result.max_fee_rate)} />
        <Row label="Баланс" value={formatUsdt(result.available_usdt)} />
        <Row label="Авторизация до" value={formatDateFromMs(result.agent_expires_at_ms)} />
      </section>

      <div className="btn-stack">
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => {
            hapticImpact('medium')
            onHome()
          }}
        >
          Открыть главную
        </button>
      </div>

      <p className="footer-note">
        Снять паузу можно на главной или в боте. Копирование начнётся с новых входов мастера.
      </p>
    </div>
  )
}
