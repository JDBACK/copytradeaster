/** Подписи шагов подключения: один источник для Home, Статуса и визарда.
 *
 * `Agent` и `Builder` остаются как есть — это имена протокола Aster, они звучат
 * в сообщениях бота и на экране подключения. `Кошелёк` и `Сверка` переведены: в
 * русском интерфейсе не должно быть `Wallet`/`Sync` рядом с русскими строками. */

export const TRUST_STEP = {
  wallet: 'Кошелёк',
  agent: 'Agent',
  builder: 'Builder',
  sync: 'Сверка',
} as const

export type TrustStepKey = keyof typeof TRUST_STEP

/** Порядок этапов на рельсе: подпись → подтверждено. */
export const TRUST_RAIL_STEPS: readonly TrustStepKey[] = ['wallet', 'agent', 'builder', 'sync']

