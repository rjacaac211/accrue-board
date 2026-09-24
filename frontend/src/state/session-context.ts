import { createContext, useContext } from 'react'

export interface Session {
  clientId: string
  actorId: string
  setClientId: (id: string) => void
  setActorId: (id: string) => void
}

export const SessionContext = createContext<Session | null>(null)

export function useSession(): Session {
  const session = useContext(SessionContext)
  if (!session) throw new Error('useSession must be used inside SessionProvider')
  return session
}
