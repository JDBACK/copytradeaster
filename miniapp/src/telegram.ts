/** Narrow Telegram WebApp SDK wrapper: the only module that touches
 * window.Telegram.  Everything degrades silently in a plain browser. */

interface TelegramWebApp {
  initData: string
  version: string
  platform: string
  ready(): void
  expand(): void
  close(): void
  setHeaderColor(color: string): void
  setBackgroundColor(color: string): void
  enableClosingConfirmation(): void
  disableClosingConfirmation(): void
  HapticFeedback?: {
    impactOccurred(style: 'light' | 'medium' | 'heavy'): void
    notificationOccurred(type: 'error' | 'success' | 'warning'): void
  }
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp }
  }
}

const CANVAS = '#070707'

export function webApp(): TelegramWebApp | null {
  return window.Telegram?.WebApp ?? null
}

export function isTelegram(): boolean {
  return Boolean(webApp()?.initData)
}

export function initData(): string {
  return webApp()?.initData ?? ''
}

export function setupTelegram(): void {
  const app = webApp()
  if (!app) return
  app.ready()
  app.expand()
  try {
    app.setHeaderColor(CANVAS)
    app.setBackgroundColor(CANVAS)
  } catch {
    /* older clients without color control */
  }
}

export function hapticImpact(style: 'light' | 'medium' | 'heavy' = 'light'): void {
  try {
    webApp()?.HapticFeedback?.impactOccurred(style)
  } catch {
    /* haptics are best-effort */
  }
}

export function hapticNotify(type: 'error' | 'success' | 'warning'): void {
  try {
    webApp()?.HapticFeedback?.notificationOccurred(type)
  } catch {
    /* haptics are best-effort */
  }
}

export function closeMiniApp(): void {
  try {
    webApp()?.close()
  } catch {
    /* nothing to close outside Telegram */
  }
}

export function setClosingConfirmation(enabled: boolean): void {
  try {
    if (enabled) webApp()?.enableClosingConfirmation()
    else webApp()?.disableClosingConfirmation()
  } catch {
    /* unsupported clients */
  }
}
