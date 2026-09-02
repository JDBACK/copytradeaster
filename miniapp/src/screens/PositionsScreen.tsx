/** Positions screen (эталон 03-positions, Stage 3 part 2 P2.1).
 * Строго read-only экран для просмотра открытых позиций фолловера.
 */

import { useCallback, useEffect, useState } from 'react'
import { api, ApiError, type AuthContext } from '../api'
import { Brand, ErrorBox } from '../components/ui'
import { describeError } from '../errors'
import { formatPnl, formatPrice, formatUsdt } from '../format'
import { snapshotLabel } from '../home'
import { PROTECTION_WARNING } from '../protectionWarning'
import type { PositionPageDTO, PositionSummaryDTO } from '../types'

const WarningIcon = ({ size }: { size: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
    <path d="M12 9v4M12 17h.01" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
    <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2" />
  </svg>
)

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; data: PositionPageDTO }
  | { kind: 'error'; code: string }

export function PositionsScreen({
  auth,
  accountMasked,
}: {
  auth: AuthContext
  accountMasked?: string | null
}) {
  const [load, setLoad] = useState<LoadState>({ kind: 'loading' })
  const [refreshing, setRefreshing] = useState(false)

  const fetchPositions = useCallback(
    async (refresh = false) => {
      if (refresh) setRefreshing(true)
      try {
        const data = await api.positions(auth, refresh)
        setLoad({ kind: 'ready', data })
      } catch (err) {
        setLoad({ kind: 'error', code: err instanceof ApiError ? err.code : 'network_error' })
      } finally {
        if (refresh) setRefreshing(false)
      }
    },
    [auth],
  )

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect
    void fetchPositions(false)
  }, [fetchPositions])

  if (load.kind === 'loading') {
    return (
      <div className="screen">
        <Brand
          right={
            accountMasked ? (
              <span className="wallet-chip num">{accountMasked}</span>
            ) : (
              <span className="badge-testnet">Aster testnet</span>
            )
          }
        />
        <div className="skeleton" style={{ minHeight: 40, width: '60%' }} />
        <div className="skeleton" style={{ minHeight: 200 }} aria-label="Загрузка позиций" />
        <div className="skeleton" style={{ minHeight: 200 }} />
      </div>
    )
  }

  if (load.kind === 'error') {
    const copy = describeError(load.code)
    return (
      <div className="screen">
        <Brand
          right={
            accountMasked ? (
              <span className="wallet-chip num">{accountMasked}</span>
            ) : (
              <span className="badge-testnet">Aster testnet</span>
            )
          }
        />
        <ErrorBox title={copy.title} hint={copy.hint} />
        <div className="btn-stack">
          <button type="button" className="btn btn-secondary" onClick={() => void fetchPositions(true)}>
            Повторить
          </button>
        </div>
      </div>
    )
  }

  const { data } = load
  const isStale = data.is_stale || data.system_state === 'stale'

  return (
    <div className="screen">
      <Brand
        right={
          accountMasked ? (
            <span className="wallet-chip num">{accountMasked}</span>
          ) : (
            <span className="badge-testnet">Aster testnet</span>
          )
        }
      />

      <div className="positions-header">
        <h1 className="h1" style={{ margin: 0 }}>
          Позиции
        </h1>
        <div className="freshness">
          <span className={`dot ${isStale ? 'warning' : 'success'}`} aria-hidden />
          {isStale ? 'Данные устарели' : 'Обновлено'}: {snapshotLabel(data.snapshot_captured_at)}
        </div>
        <div className="section-label">
          Открытые позиции · {data.count}
        </div>
      </div>

      {isStale && (
        <div className="position-warning" role="status">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
            <path d="M12 9v4M12 17h.01" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
            <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2" />
          </svg>
          <span>Снимок позиций устарел. Проверьте подключение к бирже.</span>
        </div>
      )}

      {data.positions.length === 0 ? (
        <div className="panel" style={{ textAlign: 'center', padding: '32px 16px' }}>
          <div className="tone-muted" style={{ fontSize: 15, marginBottom: 12 }}>
            Нет открытых позиций
          </div>
          <button
            type="button"
            className="btn btn-secondary btn-inline"
            disabled={refreshing}
            onClick={() => void fetchPositions(true)}
          >
            {refreshing ? 'Обновляем…' : 'Обновить'}
          </button>
        </div>
      ) : (
        data.positions.map((pos) => <PositionCard key={`${pos.symbol}-${pos.side}`} position={pos} />)
      )}

      <div className="info-box">
        Позиции только для просмотра. Управляет торговое ядро.
      </div>

      {data.positions.length > 0 && (
        <div className="btn-stack" style={{ marginTop: 4 }}>
          <button
            type="button"
            className="btn btn-secondary"
            disabled={refreshing}
            onClick={() => void fetchPositions(true)}
          >
            {refreshing ? 'Обновляем…' : 'Обновить данные'}
          </button>
        </div>
      )}
    </div>
  )
}

export function PositionCard({ position }: { position: PositionSummaryDTO }) {
  const pnl = formatPnl(position.unrealized_profit)
  const isBtc = position.symbol.toUpperCase().startsWith('BTC')
  const isEth = position.symbol.toUpperCase().startsWith('ETH')
  const coinClass = isBtc ? 'btc' : isEth ? 'eth' : ''
  const coinInitial = isBtc ? '₿' : isEth ? 'Ξ' : position.symbol.slice(0, 1)

  return (
    <section className="position-card" aria-label={`Позиция ${position.symbol} ${position.side}`}>
      <div className="position-card-header">
        <div className="position-symbol-group">
          <div className={`coin-icon ${coinClass}`} aria-hidden>
            {coinInitial}
          </div>
          <div>
            <div className="position-symbol">{position.symbol}</div>
          </div>
          <span className={`side-badge ${position.side === 'LONG' ? 'long' : 'short'}`}>
            {position.side}
          </span>
        </div>
        <div className={`position-pnl num ${pnl.tone === 'positive' ? 'pnl-positive' : pnl.tone === 'negative' ? 'pnl-negative' : 'pnl-neutral'}`}>
          {pnl.text}
        </div>
      </div>

      {PROTECTION_WARNING[position.protection_state] && (
        <div className="position-warning" role="alert">
          <WarningIcon size={16} />
          <span>{PROTECTION_WARNING[position.protection_state]}</span>
        </div>
      )}

      <div className="position-table">
        <div className="position-row">
          <span className="position-row-label">Вход</span>
          <span className="position-row-value num">{formatPrice(position.entry_price)}</span>
        </div>
        <div className="position-row">
          <span className="position-row-label">Сейчас</span>
          <span className="position-row-value num">{formatPrice(position.mark_price)}</span>
        </div>
        <div className="position-row">
          <span className="position-row-label">Stop</span>
          <span className="position-row-value num">
            {position.stop_price ? formatPrice(position.stop_price) : '—'}
          </span>
        </div>
        {position.take_profit_price && (
          <div className="position-row">
            <span className="position-row-label">Take Profit</span>
            <span className="position-row-value num">{formatPrice(position.take_profit_price)}</span>
          </div>
        )}
        <div className="position-row">
          <span className="position-row-label">Риск до Stop</span>
          <span className="position-row-value num">
            {position.risk_to_stop_usdt !== null && position.risk_to_stop_usdt !== undefined
              ? formatUsdt(position.risk_to_stop_usdt)
              : '—'}
          </span>
        </div>
        <div className="position-row">
          <span className="position-row-label">Плечо</span>
          <span className="position-row-value num">{position.leverage}× авто</span>
        </div>
      </div>
    </section>
  )
}
