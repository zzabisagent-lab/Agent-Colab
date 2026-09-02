import { BrowserRouter, Link, Navigate, Route, Routes } from 'react-router-dom'
import { LoginPage } from '../features/auth/LoginPage'
import { SessionProvider, useSession } from '../features/auth/session'
import { BridgesPage } from '../features/bridges/BridgesPage'
import { ChannelsPage } from '../features/channels/ChannelsPage'

function Shell({ children }: { children: React.ReactNode }) {
  const { me, loading, logout } = useSession()
  if (loading) return <p>Loading…</p>
  if (!me) return <Navigate to="/login" replace />
  return (
    <div className="shell">
      <a className="skip-link" href="#main">Skip to content</a>
      <header>
        <nav aria-label="Primary">
          <Link to="/channels">Channels</Link>
        </nav>
        <span>{me.account_id}</span>
        <button onClick={() => void logout()}>Sign out</button>
      </header>
      <div id="main">{children}</div>
    </div>
  )
}

export function App() {
  return (
    <SessionProvider>
      <BrowserRouter basename="/admin">
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/channels" element={<Shell><ChannelsPage /></Shell>} />
          <Route path="/channels/:channelId/bridges" element={<Shell><BridgesPage /></Shell>} />
          <Route path="*" element={<Navigate to="/channels" replace />} />
        </Routes>
      </BrowserRouter>
    </SessionProvider>
  )
}
