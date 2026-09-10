/** "Загружаем…" feedback belongs to the tap that caused it.
 *
 * A background re-read (TTL, live invalidation, reconcile) publishes the same
 * `loading` flag as a user action, so bound buttons used to flip their label and
 * dim on their own every few seconds — measured as a healthy screen changing
 * something the user did not touch. Paging runs through this hook instead, so
 * only the control the user pressed reports work.
 */

import { useState } from 'react'

export function usePaging(): [boolean, (action: () => Promise<unknown>) => void] {
  const [paging, setPaging] = useState(false)
  const start = (action: () => Promise<unknown>) => {
    setPaging(true)
    void Promise.resolve(action()).finally(() => setPaging(false))
  }
  return [paging, start]
}

