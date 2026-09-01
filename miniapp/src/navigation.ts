/** Typed navigation state for the follower app shell (UX plan §6).
 *
 * Exactly four root tabs with a persistent bottom navigation; nested screens
 * (Risk, Notifications, System Status, Account + Builder, Help) live on a
 * short stack and use the Telegram BackButton.  Pure transitions only — no
 * router dependency. */

export type RootTab = 'home' | 'positions' | 'activity' | 'settings'

export type NestedScreen =
  | { kind: 'risk' }
  | { kind: 'notifications' }
  | { kind: 'system-status' }
  | { kind: 'account-builder' }
  | { kind: 'help' }

export interface RouteState {
  tab: RootTab
  nested: NestedScreen[]
}

export const ROOT_TABS: readonly RootTab[] = ['home', 'positions', 'activity', 'settings']

export const ROOT_TAB_LABELS: Record<RootTab, string> = {
  home: 'Главная',
  positions: 'Позиции',
  activity: 'Активность',
  settings: 'Настройки',
}

export function initialRoute(): RouteState {
  return { tab: 'home', nested: [] }
}

/** Switching tabs always returns to the tab root (nested stack is dropped). */
export function selectTab(state: RouteState, tab: RootTab): RouteState {
  if (state.tab === tab && state.nested.length === 0) return state
  return { tab, nested: [] }
}

export function pushNested(state: RouteState, screen: NestedScreen): RouteState {
  return { ...state, nested: [...state.nested, screen] }
}

export function popNested(state: RouteState): RouteState {
  if (state.nested.length === 0) return state
  return { ...state, nested: state.nested.slice(0, -1) }
}

export function currentNested(state: RouteState): NestedScreen | undefined {
  return state.nested[state.nested.length - 1]
}
