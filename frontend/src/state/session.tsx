import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { SessionContext } from './session-context'

function remembered(key: string, fallback: string): string {
  try {
    return window.localStorage.getItem(key) ?? fallback
  } catch {
    return fallback
  }
}

function remember(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    // storage unavailable (private mode); the choice just isn't remembered
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [clientId, setClientId] = useState(() => remembered('accrueboard.client', ''))
  const [actorId, setActorId] = useState(() => remembered('accrueboard.actor', 'u_alex'))

  useEffect(() => remember('accrueboard.client', clientId), [clientId])
  useEffect(() => remember('accrueboard.actor', actorId), [actorId])

  const value = useMemo(
    () => ({ clientId, actorId, setClientId, setActorId }),
    [clientId, actorId],
  )
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
}
