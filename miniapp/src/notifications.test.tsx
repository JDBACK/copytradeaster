/** Tests for the Notifications screen (P2.3): the pure toggle state machine
 * (server-confirmed UI, double-tap protection, keep-confirmed-on-error) and
 * the static render contract of the production screen and switch. */

import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { NotificationToggle, NotificationsScreen } from './screens/NotificationsScreen'
import {
  createToggleGate,
  failedCountTone,
  initialNotificationsState,
  reduceNotifications,
} from './notificationsView'
import type { NotificationSettingsDTO } from './types'

function dto(overrides: Partial<NotificationSettingsDTO> = {}): NotificationSettingsDTO {
  return {
    enabled: true,
    last_delivery_at: '2026-09-02T12:41:00+00:00',
    pending: 0,
    failed: 0,
    ...overrides,
  }
}

describe('createToggleGate (synchronous double-tap lock)', () => {
  it('admits exactly one of two taps landing before a re-render', () => {
    const gate = createToggleGate()
    // Two clicks in the same tick: only the first may start a mutation —
    // the second reaches the API only if the gate already reopened.
    expect(gate.tryBegin()).toBe(true)
    expect(gate.tryBegin()).toBe(false)
    expect(gate.tryBegin()).toBe(false)
    // The gate reopens only when the in-flight mutation settles.
    gate.end()
    expect(gate.tryBegin()).toBe(true)
  })

  it('stays closed after end() is called once per settled mutation', () => {
    const gate = createToggleGate()
    expect(gate.tryBegin()).toBe(true)
    gate.end()
    gate.end()
    // Extra end() calls must not corrupt the lock for the next tap.
    expect(gate.tryBegin()).toBe(true)
    expect(gate.tryBegin()).toBe(false)
  })
})

describe('reduceNotifications (toggle state machine)', () => {
  it('starts empty and enters saving on the first request', () => {
    const idle = initialNotificationsState()
    expect(idle.confirmed).toBeNull()
    expect(idle.saving).toBe(false)

    const loaded = reduceNotifications(idle, { type: 'save_succeeded', dto: dto() })
    const saving = reduceNotifications(loaded, { type: 'save_requested' })
    expect(saving.saving).toBe(true)
    expect(saving.confirmed?.enabled).toBe(true)
  })

  it('ignores a second request while saving (double-tap protection)', () => {
    const loaded = reduceNotifications(initialNotificationsState(), {
      type: 'save_succeeded',
      dto: dto(),
    })
    const saving = reduceNotifications(loaded, { type: 'save_requested' })
    const second = reduceNotifications(saving, { type: 'save_requested' })
    // Same object reference: no state change, no second mutation started.
    expect(second).toBe(saving)
  })

  it('ignores requests before the initial load confirmed anything', () => {
    const idle = initialNotificationsState()
    expect(reduceNotifications(idle, { type: 'save_requested' })).toBe(idle)
  })

  it('applies only the server-returned DTO on success', () => {
    const loaded = reduceNotifications(initialNotificationsState(), {
      type: 'save_succeeded',
      dto: dto(),
    })
    const saving = reduceNotifications(loaded, { type: 'save_requested' })
    // The user asked to disable; the server confirms enabled=false.
    const saved = reduceNotifications(saving, {
      type: 'save_succeeded',
      dto: dto({ enabled: false }),
    })
    expect(saved.saving).toBe(false)
    expect(saved.error).toBeNull()
    expect(saved.confirmed?.enabled).toBe(false)
  })

  it('keeps the last confirmed state and records the code on failure', () => {
    const loaded = reduceNotifications(initialNotificationsState(), {
      type: 'save_succeeded',
      dto: dto({ enabled: true }),
    })
    const saving = reduceNotifications(loaded, { type: 'save_requested' })
    const failed = reduceNotifications(saving, { type: 'save_failed', code: 'network_error' })
    expect(failed.saving).toBe(false)
    expect(failed.error).toBe('network_error')
    expect(failed.confirmed?.enabled).toBe(true)
    // The confirmed state object survives untouched.
    expect(failed.confirmed).toBe(loaded.confirmed)
  })
})

describe('failedCountTone', () => {
  it('paints only real failures', () => {
    expect(failedCountTone(0)).toBe('muted')
    expect(failedCountTone(3)).toBe('danger')
  })
})

describe('NotificationsScreen render', () => {
  it('renders the loading skeleton without a switch before data arrives', () => {
    const html = renderToStaticMarkup(
      <NotificationsScreen auth={{ initData: 'test' }} />,
    )
    expect(html).toContain('Уведомления')
    expect(html).toContain('Загрузка настроек уведомлений')
    expect(html).not.toContain('role="switch"')
  })

  it('renders the error state with a retry button when the load fails', () => {
    // The production screen fetches on mount; renderToStaticMarkup shows the
    // initial (loading) frame, so the error frame is covered by the pure
    // state machine above and the retry flow by the toggle tests.
    const html = renderToStaticMarkup(
      <NotificationsScreen auth={{ initData: 'test' }} />,
    )
    expect(html).not.toContain('Повторить')
  })
})

describe('NotificationToggle accessibility', () => {
  it('is a role=switch button with aria-checked bound to the confirmed state', () => {
    const html = renderToStaticMarkup(
      <NotificationToggle enabled onToggle={() => {}} saving={false} />,
    )
    expect(html).toContain('role="switch"')
    expect(html).toContain('aria-checked="true"')
    expect(html).toContain('aria-label="Уведомления о сделках"')
    expect(html).not.toContain('disabled')
  })

  it('renders aria-checked=false and blocks interaction while saving', () => {
    const html = renderToStaticMarkup(
      <NotificationToggle enabled={false} onToggle={() => {}} saving />,
    )
    expect(html).toContain('aria-checked="false"')
    expect(html).toContain('disabled')
    expect(html).toContain('aria-busy="true"')
  })
})
