import type { LiveSnapshot } from './live'

function ageLabel(lastUpdatedAt: number, now: number): string {
  if (!lastUpdatedAt) return 'последняя сверка неизвестна'
  const seconds = Math.max(0, Math.floor((now - lastUpdatedAt) / 1000))
  if (seconds < 60) return `${seconds} сек назад`
  return `${Math.floor(seconds / 60)} мин назад`
}

export function liveStatusLabel(snapshot: LiveSnapshot, now: number): string {
  switch (snapshot.health) {
    case 'live':
      return 'LIVE'
    case 'connecting':
      return 'Подключаем live…'
    case 'reconnecting':
      return 'Восстанавливаем связь…'
    case 'stale':
      return `Нет связи · ${ageLabel(snapshot.lastUpdatedAt, now)}`
  }
}
