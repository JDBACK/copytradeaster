import { createContext, useContext } from 'react'
import type { MiniAppResources } from './resources'

export const ResourceScopeContext = createContext<MiniAppResources | null>(null)

/** Session owner for direct reads that intentionally remain screen-owned. */
export function useResourceScope(): MiniAppResources | null {
  return useContext(ResourceScopeContext)
}
