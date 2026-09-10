/** Один голос на сбой транспорта: пока rail показывает разрыв, экран не
 * повторяет ту же ошибку своей красной плашкой. Проверка идёт на настоящем
 * экране Позиций против настоящего владельца ресурсов. */

// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { api, ApiError } from './api'
import { railSpeaks, type LiveSnapshot } from './live'
import { MiniAppResources } from './resources'
import { PositionsScreen } from './screens/PositionsScreen'
import type { PositionPageDTO, PositionSummaryDTO } from './types'

const AUTH = { initData: 'test' }
const STALE: LiveSnapshot = { health: 'stale', revision: 9, lastUpdatedAt: Date.now() - 30_000, retrying: true }
const HEALTHY: LiveSnapshot = { health: 'live', revision: 9, lastUpdatedAt: Date.now(), retrying: false }

function position(overrides: Partial<PositionSummaryDTO> = {}): PositionSummaryDTO {
  return {
    symbol: 'BTCUSDT',
    side: 'LONG',
    qty: '0.050',
    entry_price: '59200.00',
    mark_price: '60150.50',
    unrealized_profit: '47.525',
    leverage: 20,
    protection_state: 'protected',
    stop_price: '58000.00',
    take_profit_price: '64000.00',
    risk_to_stop_usdt: '60.060',
    ...overrides,
  }
}

function positionPage(): PositionPageDTO {
  return {
    positions: [position()],
    count: 1,
    system_state: 'ready',
    snapshot_captured_at: '2026-09-02T12:00:00+00:00',
    is_stale: false,
  }
}

function outageResources(health: 'stale' | 'live'): MiniAppResources {
  const resources = new MiniAppResources(AUTH, `outage-voice-${health}`)
  resources.getLiveSnapshot = () => (health === 'stale' ? STALE : HEALTHY)
  return resources
}

/** Last good data on screen, then a read that fails. */
async function withFailedRead(resources: MiniAppResources, withData: boolean) {
  if (withData) resources.publishConfirmed('positions', positionPage())
  vi.spyOn(api, 'positions').mockRejectedValue(new ApiError(0, 'network_error', 'offline'))
  await resources.refresh('positions', 'strong').catch(() => {})
  expect(resources.getSnapshot('positions').error).not.toBeNull()
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('опоткнувшийся транспорт говорит один раз', () => {
  it('rail сообщает о разрыве, экран не повторяет плашку', async () => {
    const resources = outageResources('stale')
    await withFailedRead(resources, true)
    render(<PositionsScreen auth={AUTH} resources={resources} />)
    expect(screen.getByText(/BTCUSDT/)).toBeInTheDocument()
    expect(screen.queryByText(/Проверьте интернет/i)).toBeNull()
  })

  it('без rail ошибка чтения объясняется плашкой', async () => {
    const resources = outageResources('live')
    await withFailedRead(resources, true)
    render(<PositionsScreen auth={AUTH} resources={resources} />)
    expect(screen.getByText(/Проверьте интернет/i)).toBeInTheDocument()
  })

  it('когда показывать нечего, плашка остаётся единственным объяснением', async () => {
    const resources = outageResources('stale')
    await withFailedRead(resources, false)
    render(<PositionsScreen auth={AUTH} resources={resources} />)
    expect(screen.getByText(/Проверьте интернет/i)).toBeInTheDocument()
  })
})

describe('railSpeaks', () => {
  it('молчит на переподключении и на здоровом канале', () => {
    expect(railSpeaks({ ...HEALTHY, health: 'reconnecting' }, true)).toBe(false)
    expect(railSpeaks(HEALTHY, true)).toBe(false)
  })

  it('не забирает у экрана ошибку, когда данных нет', () => {
    expect(railSpeaks(STALE, false)).toBe(false)
    expect(railSpeaks(STALE, true)).toBe(true)
  })
})

describe('предупреждение о снимке не повторяет rail', () => {
  it('при разрыве говорит только rail, карточка не скрывает реальный стоп', async () => {
    const resources = outageResources('stale')
    resources.publishConfirmed('positions', positionPage())
    render(<PositionsScreen auth={AUTH} resources={resources} />)
    expect(screen.queryByText(/Снимок позиций не обновляется/i)).toBeNull()
    expect(screen.queryByText('Статус стопа не обновлён')).toBeNull()
    expect(screen.getByText(/подтверждён/)).toBeInTheDocument()
  })

  it('снимок, признанный устаревшим движком, говорит сам', async () => {
    const resources = outageResources('live')
    resources.publishConfirmed('positions', { ...positionPage(), is_stale: true })
    render(<PositionsScreen auth={AUTH} resources={resources} />)
    expect(screen.getByText(/Снимок позиций не обновляется/i)).toBeInTheDocument()
  })

  it('во время разрыва движковый флаг уступает место rail', async () => {
    const resources = outageResources('stale')
    resources.publishConfirmed('positions', { ...positionPage(), is_stale: true })
    render(<PositionsScreen auth={AUTH} resources={resources} />)
    expect(screen.queryByText(/Снимок позиций не обновляется/i)).toBeNull()
  })
})
