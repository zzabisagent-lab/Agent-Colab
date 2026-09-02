import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { ApiError, del, get, post } from '../../api/client'

export interface Me {
  account_id: string
  account_type: string
  credential_kind: string
  mfa_verified: boolean
}

interface SessionState {
  me: Me | null
  loading: boolean
  login: (serviceToken: string) => Promise<void>
  logout: () => Promise<void>
}

const SessionContext = createContext<SessionState | null>(null)

export function SessionProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null)
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    get<Me>('/api/v1/auth/me').then(setMe).catch(() => setMe(null)).finally(() => setLoading(false))
  }, [])
  const login = useCallback(async (serviceToken: string) => {
    await post('/api/v1/auth/sessions', { service_token: serviceToken })
    setMe(await get<Me>('/api/v1/auth/me'))
  }, [])
  const logout = useCallback(async () => {
    try { await del('/api/v1/auth/sessions') } catch (e) { if (!(e instanceof ApiError)) throw e }
    setMe(null)
  }, [])
  const value = useMemo(() => ({ me, loading, login, logout }), [me, loading, login, logout])
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
}

export function useSession(): SessionState {
  const ctx = useContext(SessionContext)
  if (!ctx) throw new Error('SessionProvider missing')
  return ctx
}
