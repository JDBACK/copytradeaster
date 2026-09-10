/** Кнопка пагинации сообщает о работе только по собственному нажатию: фоновая
 * перечитка (TTL, live-инвалидация) раньше переключала подпись в «Загружаем…» и
 * гасила кнопку — на здоровом экране это выглядело как работа приложения со
 * списком, которого пользователь не касался. */

// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api } from './api'
import { MiniAppResources } from './resources'
import { ActivityScreen } from './screens/ActivityScreen'
import type { ActivityFeedPage } from './types'

const AUTH = { initData: 'test' }

function feed(overrides: Partial<ActivityFeedPage> = {}): ActivityFeedPage {
  return {
    items: [
      {
        event_ref: 'evt-1',
        kind: 'COPY_RESUMED',
        trade_ref: null,
        occurred_at: '2026-09-10T12:00:00Z',
        facts: {},
        text: 'Копирование возобновлено.',
      },
    ],
    next_cursor: 'v1:next',
    high_water: 1,
    latest_seq: 1,
    pending: [],
    ...overrides,
  }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('обратная связь по нажатию, а не по фоновой работе', () => {
  it('фоновая перечитка не переключает кнопку в «Загружаем…»', async () => {
    const resources = new MiniAppResources(AUTH, 'paging-quiet')
    let release: (page: ActivityFeedPage) => void = () => {}
    vi.spyOn(api, 'activityFeed')
      .mockResolvedValueOnce(feed())
      .mockImplementationOnce(
        () => new Promise<ActivityFeedPage>((resolve) => { release = resolve }),
      )

    render(<ActivityScreen auth={AUTH} resources={resources} />)
    await screen.findByRole('button', { name: 'Показать ещё' })

    act(() => {
      void resources.history
        .load({ kind: 'feed', filter: 'all' }, 'refresh')
        .catch(() => {})
    })
    await waitFor(() =>
      expect(resources.history.get({ kind: 'feed', filter: 'all' }).loading).toBe(true),
    )
    expect(screen.getByRole('button', { name: 'Показать ещё' })).toBeEnabled()

    fireEvent.click(screen.getByRole('button', { name: 'Показать ещё' }))
    expect(await screen.findByRole('button', { name: 'Загружаем…' })).toBeDisabled()

    await act(async () => {
      release(feed({ next_cursor: null }))
    })
    await waitFor(() => expect(screen.queryByRole('button', { name: /Загружаем/ })).toBeNull())
  })
})

