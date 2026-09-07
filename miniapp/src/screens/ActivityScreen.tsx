/** Activity screen (эталон 04-activity, Stage 3 part 2 P2.2).
 *
 * Read-only timeline of recent execution journal events. The backend is the
 * single source of truth for mapping journal kinds/statuses into the safe
 * ActivityItemDTO view-model; the UI never sees command_key, account_id,
 * addresses, nonces, client_order_id, raw Aster codes, payloads, or any
 * internal exception text.
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { ACTIVITY_REVALIDATE_MS, useWarmRevalidation, type WarmReason } from '../revalidate'
import { api, ApiError, type AuthContext } from '../api'
import {
  ACTIVITY_TIME_LABELS,
  activityEmptyCopy,
  activityStatusLabel,
  activityStatusTone,
  groupActivityByDay,
} from '../activityView'
import { Brand, ErrorBox } from '../components/ui'
import { describeError } from '../errors'
import { formatTime } from '../format'
import { hapticImpact } from '../telegram'
import type { ActivityFilter, ActivityItemDTO, ActivityPageDTO, ActivityStatus } from '../types'
import { useResourceScope } from '../resourceScopeContext'

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; data: ActivityPageDTO; items: ActivityItemDTO[]; page: number }
  | { kind: 'error'; code: string }

const FILTERS: { id: ActivityFilter; label: string }[] = [
  { id: 'all', label: 'Все' },
  { id: 'trade', label: 'Сделки' },
  { id: 'system', label: 'Система' },
]

export function ActivityScreen({
  auth,
  accountMasked,
  active = true,
  warm = true,
  invalidateSignal,
  foregroundSignal,
}: {
  auth: AuthContext
  accountMasked?: string | null
  active?: boolean
  /** Gate for the first fetch: shell warmup or the user's first tap. */
  warm?: boolean
  /** Shell-owned mutation counter; see useWarmRevalidation. */
  invalidateSignal?: number
  /** App-wide foreground edge; active Activity re-checks its TTL on change. */
  foregroundSignal?: number
}) {
  const [load, setLoad] = useState<LoadState>({ kind: 'loading' })
  const [filter, setFilter] = useState<ActivityFilter>('all')
  const [loadingMore, setLoadingMore] = useState(false)
  const [now] = useState(() => new Date())
  const seq = useRef(0)
  const abortRef = useRef<AbortController | null>(null)
  const fetchedRef = useRef<{ filter: ActivityFilter } | null>(null)
  const lifecycleLeaseRef = useRef(0)
  const sessionResources = useResourceScope()
  const scopeOwnerRef = useRef(sessionResources)

  const groups = useMemo(
    () => groupActivityByDay(load.kind === 'ready' ? load.items : [], now),
    [load, now],
  )
  const { markFresh } = useWarmRevalidation({
    active,
    ttlMs: ACTIVITY_REVALIDATE_MS,
    busy: load.kind === 'loading' || loadingMore,
    invalidateSignal,
    foregroundSignal,
    onRefresh: (reason: WarmReason) => {
      // Revalidation must not reset the client-side pagination position:
      // a silent page-0 refresh replaces the accumulated timeline, so it
      // only applies while nothing was appended yet. A paginated timeline
      // skips the refresh; the next activation past TTL retries the skip.
      // The same guard covers invalidation: appended pages are never reset.
      if (load.kind === 'ready' && load.page > 0) return undefined
      return fetchPage(filter, 0, false, reason === 'revalidate')
    },
  })

  const fetchPage = useCallback(
    async (active: ActivityFilter, page: number, append: boolean, silent = false) => {
      const my = ++seq.current
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      try {
        const data = await api.activity(auth, active, page, { signal: controller.signal })
        if (seq.current !== my) return
        markFresh()
        setLoad((prev) => {
          if (!append) {
            return { kind: 'ready', data, items: [...data.items], page }
          }
          if (prev.kind !== 'ready') {
            return { kind: 'ready', data, items: [...data.items], page }
          }
          return { ...prev, data, items: [...prev.items, ...data.items], page }
        })
      } catch (err) {
        if (sessionResources?.handleInvalidSessionError(err)) return
        if (err instanceof ApiError && err.code === 'aborted') return
        if (seq.current !== my) return
        if (append || silent) {
          // Keep the previous page visible; surface failure on the next manual
          // load.  Avoids wiping already-displayed timeline rows. A silent
          // background refresh retries automatically on the next activation
          // past TTL.
        } else {
          setLoad({ kind: 'error', code: err instanceof ApiError ? err.code : 'network_error' })
        }
      } finally {
        if (seq.current === my) setLoadingMore(false)
      }
    },
    [auth, markFresh, sessionResources],
  )

  useLayoutEffect(() => {
    if (scopeOwnerRef.current === sessionResources) return
    scopeOwnerRef.current = sessionResources

    // Activity remains screen-owned in S2, so resource-owner replacement must
    // establish its generation boundary manually. Fence and abort before paint,
    // then remove all old-scope admission/state before launching fresh truth.
    seq.current += 1
    abortRef.current?.abort()
    abortRef.current = null
    fetchedRef.current = null
    lifecycleLeaseRef.current += 1
    setLoadingMore(false)
    setLoad({ kind: 'loading' })

    // A new connected owner always gets its own page-0 read, even when this
    // keep-alive root is hidden. Mark admission first so the passive warm/filter
    // effect cannot duplicate the request.
    fetchedRef.current = { filter }
    void fetchPage(filter, 0, false, false)
  }, [fetchPage, filter, sessionResources])

  useEffect(() => {
    const lease = ++lifecycleLeaseRef.current
    // First fetch only: shell warmup or the user's first tap, whichever is
    // earlier; filter changes below refetch through the same gate record.
    if (warm && (!fetchedRef.current || fetchedRef.current.filter !== filter)) {
      fetchedRef.current = { filter }
      // oxlint-disable-next-line react/set-state-in-effect
      setLoad({ kind: 'loading' })
      void fetchPage(filter, 0, false, false)
    }
    const controller = abortRef.current
    return () => {
      // React StrictMode performs cleanup→setup synchronously. Defer only the
      // transport abort: a second setup bumps the lease and keeps the first
      // shared initial request alive; a real unmount has no successor and aborts.
      queueMicrotask(() => {
        // oxlint-disable-next-line react-hooks/exhaustive-deps
        if (lifecycleLeaseRef.current === lease) controller?.abort()
      })
    }
  }, [warm, filter, fetchPage])

  const handleFilter = useCallback((next: ActivityFilter) => {
    setFilter(next)
    hapticImpact('light')
  }, [])

  const handleLoadMore = useCallback(() => {
    if (load.kind !== 'ready' || !load.data.has_next || loadingMore) return
    setLoadingMore(true)
    void fetchPage(filter, load.page + 1, true)
  }, [filter, load, loadingMore, fetchPage])

  const handleRetry = useCallback(() => {
    setLoad({ kind: 'loading' })
    void fetchPage(filter, 0, false)
  }, [filter, fetchPage])

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
        <h1 className="h1" style={{ marginTop: 4 }}>
          Активность
        </h1>
        <div className="skeleton" style={{ minHeight: 40, width: '60%' }} />
        <div className="skeleton" style={{ minHeight: 240 }} aria-label="Загрузка журнала" />
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
        <h1 className="h1" style={{ marginTop: 4 }}>
          Активность
        </h1>
        <ErrorBox title={copy.title} hint={copy.hint} />
        <div className="btn-stack">
          <button type="button" className="btn btn-secondary" onClick={handleRetry}>
            Повторить
          </button>
        </div>
      </div>
    )
  }

  const { items, data, page } = load

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

      <h1 className="h1" style={{ marginTop: 4 }}>
        Активность
      </h1>
      <div className="lede tone-muted">Безопасный журнал действий</div>

      <div className="segmented" role="tablist" aria-label="Фильтр журнала">
        {FILTERS.map((entry) => {
          const isActive = entry.id === data.filter
          return (
            <button
              key={entry.id}
              type="button"
              role="tab"
              aria-selected={isActive}
              className={`segmented-item${isActive ? ' active' : ''}`}
              onClick={() => handleFilter(entry.id)}
            >
              {entry.label}
            </button>
          )
        })}
      </div>

      {items.length === 0 ? (
        <div className="panel" style={{ textAlign: 'center', padding: '32px 16px' }}>
          <div className="tone-muted" style={{ fontSize: 15, marginBottom: 12 }}>
            {activityEmptyCopy(data.filter)}
          </div>
        </div>
      ) : (
        <div className="activity-timeline" aria-label="Хронология действий">
          {groups.map((group) => (
            <section key={group.bucket} aria-label={ACTIVITY_TIME_LABELS[group.bucket]}>
              <div className="activity-day-label">{ACTIVITY_TIME_LABELS[group.bucket]}</div>
              {group.items.map((item, index) => (
                <ActivityRow
                  key={`${item.occurred_at ?? ''}-${item.title}-${index}`}
                  item={item}
                  isLast={index === group.items.length - 1}
                />
              ))}
            </section>
          ))}
        </div>
      )}

      {data.has_next && items.length > 0 && (
        <div className="btn-stack" style={{ marginTop: 4 }}>
          <button
            type="button"
            className="btn btn-secondary"
            disabled={loadingMore}
            onClick={handleLoadMore}
          >
            {loadingMore ? 'Загружаем…' : 'Показать ещё'}
          </button>
        </div>
      )}

      <div className="info-box activity-safe-note">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
          <path
            d="M12 3.5 19 6v5.2c0 4.6-2.9 7.6-7 9.3-4.1-1.7-7-4.7-7-9.3V6l7-2.5Z"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinejoin="round"
          />
        </svg>
        <span>Показаны только данные без секретов</span>
      </div>

      {/* Pagination state is kept on the client for accessibility; the visible
          page number is not exposed in the UI.  Tests assert that data.page
          advances after each load-more request. */}
      <span data-activity-page={page} hidden />
    </div>
  )
}

function ActivityRow({ item, isLast }: { item: ActivityItemDTO; isLast: boolean }) {
  const tone = activityStatusTone(item)
  return (
    <article
      className={`activity-row tone-${tone}`}
      aria-label={`${item.title} — ${activityStatusLabel(item)}`}
    >
      <div className="activity-time num">{formatTime(item.occurred_at)}</div>
      <div className="activity-marker" aria-hidden>
        <StatusGlyph status={item.status} />
        {!isLast && <span className="activity-connector" />}
      </div>
      <div className="activity-body">
        <div className="activity-title">{item.title}</div>
        <div className={`activity-status tone-${tone}`}>{activityStatusLabel(item)}</div>
        {item.remediation && (
          <div className="activity-remediation tone-muted">{item.remediation}</div>
        )}
      </div>
    </article>
  )
}

function StatusGlyph({ status }: { status: ActivityStatus }) {
  if (status === 'completed') {
    return (
      <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden>
        <circle cx="10" cy="10" r="8.4" stroke="var(--success)" strokeWidth="1.6" fill="none" />
        <path
          d="m6.4 10.3 2.4 2.4 4.8-5"
          stroke="var(--success)"
          strokeWidth="1.9"
          strokeLinecap="round"
          strokeLinejoin="round"
          fill="none"
        />
      </svg>
    )
  }
  if (status === 'pending') {
    return (
      <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden>
        <circle cx="10" cy="10" r="8.4" stroke="var(--warning)" strokeWidth="1.6" fill="none" />
        <circle cx="10" cy="10" r="2.4" fill="var(--warning)" />
      </svg>
    )
  }
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden>
      <circle cx="10" cy="10" r="8.4" stroke="var(--danger)" strokeWidth="1.6" fill="none" />
      <path
        d="m7 7 6 6m0-6-6 6"
        stroke="var(--danger)"
        strokeWidth="1.9"
        strokeLinecap="round"
        fill="none"
      />
    </svg>
  )
}
