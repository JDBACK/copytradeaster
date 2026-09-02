/** Pure Notifications toggle state machine (эталон 06-settings, P2.3).
 *
 * The server response is the single source of truth: the UI flips only when
 * the returned DTO confirms it.  While a mutation is in flight further
 * toggle requests are ignored (double-tap protection), and a failed save
 * keeps the last confirmed state visible. */

import type { NotificationSettingsDTO } from './types'

export interface NotificationsState {
  /** Last server-confirmed settings; null until the initial load lands. */
  confirmed: NotificationSettingsDTO | null
  saving: boolean
  /** Save-failure code; null when the confirmed state is clean. */
  error: string | null
}

export type NotificationsEvent =
  | { type: 'save_requested' }
  | { type: 'save_succeeded'; dto: NotificationSettingsDTO }
  | { type: 'save_failed'; code: string }

export function initialNotificationsState(): NotificationsState {
  return { confirmed: null, saving: false, error: null }
}

export function reduceNotifications(
  state: NotificationsState,
  event: NotificationsEvent,
): NotificationsState {
  switch (event.type) {
    case 'save_requested': {
      // One mutation at a time: a second tap while saving is a no-op.
      if (state.saving || state.confirmed === null) return state
      return { ...state, saving: true, error: null }
    }
    case 'save_succeeded':
      return { confirmed: event.dto, saving: false, error: null }
    case 'save_failed':
      // Keep the last confirmed toggle state on failure.
      return { ...state, saving: false, error: event.code }
    default:
      return state
  }
}

/** Synchronous in-flight lock for the toggle.  React state (`saving`) exists
 * only after the next render, so two taps landing in the same tick would both
 * pass a state-only check and both reach the API.  The gate closes before the
 * API call starts and reopens when the mutation settles: exactly one mutation
 * per tap burst. */
export interface ToggleGate {
  /** Admits the click that starts a mutation; rejects the rest until end(). */
  tryBegin: () => boolean
  /** Reopens the gate when the in-flight mutation settles. */
  end: () => void
}

export function createToggleGate(): ToggleGate {
  let inFlight = false
  return {
    tryBegin: () => {
      if (inFlight) return false
      inFlight = true
      return true
    },
    end: () => {
      inFlight = false
    },
  }
}

/** Safe delivery lines: counts as-is, timestamps rendered by formatTime. */
export function failedCountTone(failed: number): 'muted' | 'danger' {
  return failed > 0 ? 'danger' : 'muted'
}
