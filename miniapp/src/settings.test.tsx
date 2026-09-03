/** Tests for the Settings root (эталон 06-settings, P2.3): pure view-model
 * mappings plus the static render contract of the production SettingsView
 * (rows, chevrons only on real navigation, no fake attestation). */

import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { SettingsView } from './screens/SettingsScreen'
import {
  copySummary,
  notificationsValue,
  systemStatusValue,
} from './settingsView'
import type { DashboardDTO } from './types'

function makeDashboard(overrides: Partial<DashboardDTO> = {}): DashboardDTO {
  return {
    copy_state: 'active',
    system_state: 'ready',
    account_masked: '0x3c45…F975',
    risk_percent: '0.01',
    open_positions: 0,
    snapshot_captured_at: '2026-09-02T12:00:00+00:00',
    notification_enabled: true,
    is_admin: false,
    ...overrides,
  }
}

describe('copySummary', () => {
  it('maps copying states to Russian labels with honest tones', () => {
    expect(copySummary('active')).toEqual({ label: 'Включено', tone: 'success' })
    expect(copySummary('paused')).toEqual({ label: 'На паузе', tone: 'warning' })
    expect(copySummary('degraded')).toEqual({ label: 'Требует проверки', tone: 'warning' })
    expect(copySummary('blocked')).toEqual({ label: 'Остановлено', tone: 'danger' })
    expect(copySummary('empty')).toEqual({ label: 'Не подключено', tone: 'muted' })
  })
})

describe('systemStatusValue', () => {
  const SNAPSHOT = '2026-09-02T12:00:00+00:00'

  it('reads «Всё работает» only for ready with a captured snapshot', () => {
    expect(systemStatusValue('ready', SNAPSHOT)).toEqual({ label: 'Всё работает', tone: 'success' })
  })

  it('shows «Ожидание данных» for ready without a snapshot (missing/null/empty)', () => {
    for (const snapshot of [undefined, null, '']) {
      const waiting = systemStatusValue('ready', snapshot)
      expect(waiting.label).toBe('Ожидание данных')
      expect(waiting.tone).toBe('muted')
      expect(waiting.label).not.toBe('Всё работает')
    }
  })

  it('keeps stale honest even when a snapshot exists', () => {
    const stale = systemStatusValue('stale', SNAPSHOT)
    expect(stale).toEqual({ label: 'Данные устарели', tone: 'warning' })
    expect(systemStatusValue('degraded', SNAPSHOT).tone).toBe('danger')
    expect(systemStatusValue('blocked', SNAPSHOT).tone).toBe('danger')
    expect(systemStatusValue('unknown', SNAPSHOT).tone).toBe('muted')
  })
})

describe('notificationsValue', () => {
  it('maps the dashboard flag to Включены/Отключены', () => {
    expect(notificationsValue(true)).toEqual({ label: 'Включены', tone: 'success' })
    expect(notificationsValue(false)).toEqual({ label: 'Отключены', tone: 'muted' })
  })
})

describe('SettingsView render (эталон 06-settings)', () => {
  const onOpen = () => {}

  it('renders all capability rows with sections in the reference order', () => {
    const html = renderToStaticMarkup(
      <SettingsView dashboard={makeDashboard()} accountMasked="0x3c45…F975" onOpen={onOpen} />,
    )
    expect(html).toContain('Настройки')
    expect(html).toContain('Копирование')
    expect(html).toContain('Настройки копирования')
    expect(html).toContain('Риск на Stop')
    expect(html).toContain('Уведомления')
    expect(html).toContain('Статус Aster')
    expect(html).toContain('Подключение и безопасность')
    expect(html).toContain('Аккаунт и Builder')
    expect(html).toContain('Безопасность')
    expect(html).toContain('Поддержка')
    expect(html).toContain('Помощь')
    expect(html).toContain('Среда')
    expect(html).toContain('Aster testnet')
  })

  it('derives values from the authoritative dashboard DTO, not hardcode', () => {
    const html = renderToStaticMarkup(
      <SettingsView dashboard={makeDashboard()} accountMasked={null} onOpen={onOpen} />,
    )
    // risk_percent 0.01 → 1% via the shared formatter.
    expect(html).toContain('1%')
    expect(html).toContain('Включены')
    expect(html).toContain('Всё работает')
    expect(html).toContain('0x3c45…F975')
  })

  it('never paints stale/degraded as «Всё работает»', () => {
    const html = renderToStaticMarkup(
      <SettingsView
        dashboard={makeDashboard({ system_state: 'stale' })}
        accountMasked={null}
        onOpen={onOpen}
      />,
    )
    expect(html).toContain('Данные устарели')
    expect(html).not.toContain('Всё работает')
  })

  it('never claims «Всё работает» for ready without a captured snapshot', () => {
    const html = renderToStaticMarkup(
      <SettingsView
        dashboard={makeDashboard({ snapshot_captured_at: null })}
        accountMasked={null}
        onOpen={onOpen}
      />,
    )
    expect(html).toContain('Ожидание данных')
    expect(html).not.toContain('Всё работает')
  })

  it('shows the neutral Builder state, not «Проверено»', () => {
    const html = renderToStaticMarkup(
      <SettingsView dashboard={makeDashboard()} accountMasked="0x3c45…F975" onOpen={onOpen} />,
    )
    expect(html).not.toContain('Проверено')
  })

  it('gives chevrons (buttons) only to the six navigable rows', () => {
    const html = renderToStaticMarkup(
      <SettingsView dashboard={makeDashboard()} accountMasked={null} onOpen={onOpen} />,
    )
    // Риск, Уведомления, Статус Aster, Аккаунт и Builder, Безопасность,
    // Помощь — и никаких других кнопок в списке.
    const buttons = html.match(/<button[^>]*class="settings-row"/g) ?? []
    expect(buttons).toHaveLength(6)
    // The static environment row stays a div.
    expect(html).toContain('class="settings-row"')
  })

  it('keeps the environment row static without a chevron', () => {
    const html = renderToStaticMarkup(
      <SettingsView dashboard={makeDashboard()} accountMasked={null} onOpen={onOpen} />,
    )
    expect(html).toContain('Среда')
    expect(html).toContain('Aster testnet')
    // The three buttons are exactly Уведомления/Безопасность/Помощь (see the
    // chevron test), so «Среда» cannot be a navigation affordance.
  })

  it('exposes accessible names for the summary state and rows', () => {
    const html = renderToStaticMarkup(
      <SettingsView dashboard={makeDashboard()} accountMasked={null} onOpen={onOpen} />,
    )
    expect(html).toContain('aria-label="Копирование: Включено"')
  })

  it('leaks no internal identifiers into the markup', () => {
    const html = renderToStaticMarkup(
      <SettingsView dashboard={makeDashboard()} accountMasked={null} onOpen={onOpen} />,
    )
    const lower = html.toLowerCase()
    for (const forbidden of [
      'account_id',
      'command_key',
      'client_order_id',
      'private_key',
      'nonce',
      'payload',
    ]) {
      expect(lower).not.toContain(forbidden)
    }
  })
})
