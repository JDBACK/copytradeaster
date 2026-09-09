// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest'
import { act } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api, ApiError } from './api'
import { MiniAppResources } from './resources'
import { NotificationsScreen } from './screens/NotificationsScreen'
import type { NotificationSettingsDTO } from './types'

const AUTH = { initData: 'test' }

function dto(overrides: Partial<NotificationSettingsDTO> = {}): NotificationSettingsDTO {
  return {
    enabled: true,
    last_delivery_at: null,
    pending: 0,
    failed: 0,
    ...overrides,
  }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('NotificationsScreen hard session boundary', () => {
  it('routes direct mutation invalid_session through the connected resource owner', async () => {
    const onInvalidSession = vi.fn()
    const resources = new MiniAppResources(AUTH, 'notifications-invalid-session', onInvalidSession)
    resources.publishConfirmed('notifications', dto())
    vi.spyOn(api, 'setNotifications').mockRejectedValue(
      new ApiError(400, 'invalid_session', 'expired session'),
    )

    render(<NotificationsScreen auth={AUTH} resources={resources} />)
    fireEvent.click(screen.getByRole('switch', { name: 'Уведомления о сделках' }))

    await act(async () => {})
    expect(onInvalidSession).toHaveBeenCalledTimes(1)
    expect(resources.getSnapshot('notifications').data).toBeNull()
    expect(resources.mutationLocked('notifications')).toBe(false)
    expect(screen.queryByText(/настройка не изменилась/i)).toBeNull()
  })

  it('keeps cached Notifications visibly stale and disables mutation until refresh', () => {
    const resources = new MiniAppResources(AUTH, 'notifications-stale-honesty')
    resources.publishConfirmed('notifications', dto())
    resources.invalidate(['notifications'])
    vi.spyOn(api, 'notifications').mockReturnValue(new Promise<NotificationSettingsDTO>(() => {}))

    render(<NotificationsScreen auth={AUTH} resources={resources} />)
    expect(screen.getByText(/данные требуют проверки перед изменением/i)).toBeDefined()
    expect(screen.getByRole('switch', { name: 'Уведомления о сделках' })).toBeDisabled()
  })

  it('retains double-unknown Notifications across remount until authoritative recovery without replay', async () => {
    const resources = new MiniAppResources(AUTH, 'notifications-double-unknown')
    resources.publishConfirmed('notifications', dto())
    const setter = vi
      .spyOn(api, 'setNotifications')
      .mockRejectedValue(new ApiError(0, 'network_error', 'response lost'))
    const reader = vi
      .spyOn(api, 'notifications')
      .mockRejectedValueOnce(new ApiError(0, 'network_error', 'verify lost'))
      .mockResolvedValueOnce(dto())

    const first = render(<NotificationsScreen auth={AUTH} resources={resources} />)
    fireEvent.click(screen.getByRole('switch', { name: 'Уведомления о сделках' }))
    await waitFor(() => expect(resources.mutationState('notifications')).toBe('unknown'))
    expect(setter).toHaveBeenCalledTimes(1)
    first.unmount()

    render(<NotificationsScreen auth={AUTH} resources={resources} />)
    expect(screen.getByRole('switch', { name: 'Уведомления о сделках' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Обновить данные' }))
    await waitFor(() => expect(reader).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(resources.mutationState('notifications')).toBeNull())
    expect(setter).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('switch', { name: 'Уведомления о сделках' })).toBeEnabled()
  })
})
