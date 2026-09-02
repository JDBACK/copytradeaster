import { describe, expect, it } from 'vitest'
import {
  ROOT_TABS,
  ROOT_TAB_LABELS,
  currentNested,
  initialRoute,
  popNested,
  pushNested,
  selectTab,
} from './navigation'

describe('app shell navigation', () => {
  it('has exactly four root tabs in the UX-plan order', () => {
    expect(ROOT_TABS).toEqual(['home', 'positions', 'activity', 'settings'])
    expect(ROOT_TABS).toHaveLength(4)
  })

  it('labels every root tab in Russian', () => {
    expect(ROOT_TAB_LABELS.home).toBe('Главная')
    expect(ROOT_TAB_LABELS.positions).toBe('Позиции')
    expect(ROOT_TAB_LABELS.activity).toBe('Активность')
    expect(ROOT_TAB_LABELS.settings).toBe('Настройки')
  })

  it('starts on the home tab without nested screens', () => {
    const route = initialRoute()
    expect(route.tab).toBe('home')
    expect(currentNested(route)).toBeUndefined()
  })

  it('switches tabs and drops the nested stack', () => {
    let route = initialRoute()
    route = pushNested(route, { kind: 'risk' })
    route = pushNested(route, { kind: 'notifications' })
    route = selectTab(route, 'activity')
    expect(route.tab).toBe('activity')
    expect(currentNested(route)).toBeUndefined()
  })

  it('re-selecting the current tab is a no-op', () => {
    const route = initialRoute()
    expect(selectTab(route, 'home')).toBe(route)
  })

  it('push/pop walks the nested stack like Telegram BackButton', () => {
    let route = selectTab(initialRoute(), 'settings')
    route = pushNested(route, { kind: 'account-builder' })
    expect(currentNested(route)).toEqual({ kind: 'account-builder' })
    route = popNested(route)
    expect(currentNested(route)).toBeUndefined()
    expect(route.tab).toBe('settings')
    expect(popNested(route)).toBe(route)
  })

  it('P2.3 settings opens notifications/security/help on the stack', () => {
    let route = selectTab(initialRoute(), 'settings')
    route = pushNested(route, { kind: 'notifications' })
    expect(currentNested(route)).toEqual({ kind: 'notifications' })
    route = popNested(route)
    route = pushNested(route, { kind: 'security' })
    expect(currentNested(route)).toEqual({ kind: 'security' })
    route = popNested(route)
    route = pushNested(route, { kind: 'help' })
    expect(currentNested(route)).toEqual({ kind: 'help' })
    route = popNested(route)
    expect(route.tab).toBe('settings')
  })
})
