import { BrowserRouter, Link, Navigate, Route, Routes } from 'react-router-dom'
import { AccountsPage } from '../features/accounts/AccountsPage'
import { AgentsPage } from '../features/agents/AgentsPage'
import { ApprovalsQueuePage } from '../features/approvals/ApprovalsQueuePage'
import { AuditPage } from '../features/audit/AuditPage'
import { LoginPage } from '../features/auth/LoginPage'
import { MaintenancePage } from '../features/maintenance/MaintenancePage'
import { MfaPage } from '../features/mfa/MfaPage'
import { OverviewPage } from '../features/overview/OverviewPage'
import { SessionProvider, useSession } from '../features/auth/session'
import { BridgesPage } from '../features/bridges/BridgesPage'
import { ChannelsPage } from '../features/channels/ChannelsPage'
import { RolesPage } from '../features/roles/RolesPage'
import { SchedulesPage } from '../features/schedules/SchedulesPage'
import { SecretsPage } from '../features/secrets/SecretsPage'
import { SettingsPage } from '../features/settings/SettingsPage'
import { SetupWizardPage } from '../features/setup/SetupWizardPage'

function Shell({ children }: { children: React.ReactNode }) {
  const { me, loading, logout } = useSession()
  if (loading) return <p>Loading…</p>
  if (!me) return <Navigate to="/login" replace />
  return (
    <div className="shell">
      <a className="skip-link" href="#main">Skip to content</a>
      <header>
        <nav aria-label="Primary">
          <Link to="/overview">Overview</Link>
          <Link to="/channels">Channels</Link>
          <Link to="/agents">Agents</Link>
          <Link to="/roles">Roles</Link>
          <Link to="/accounts">Accounts</Link>
          <Link to="/secrets">Secrets</Link>
          <Link to="/approvals">Approvals</Link>
          <Link to="/schedules">Schedules</Link>
          <Link to="/settings">Settings</Link>
          <Link to="/audit">Audit</Link>
          <Link to="/maintenance">Maintenance</Link>
          <Link to="/mfa">MFA</Link>
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
          <Route path="/agents" element={<Shell><AgentsPage /></Shell>} />
          <Route path="/roles" element={<Shell><RolesPage /></Shell>} />
          <Route path="/setup" element={<SetupWizardPage />} />
          <Route path="/overview" element={<Shell><OverviewPage /></Shell>} />
          <Route path="/accounts" element={<Shell><AccountsPage /></Shell>} />
          <Route path="/secrets" element={<Shell><SecretsPage /></Shell>} />
          <Route path="/approvals" element={<Shell><ApprovalsQueuePage /></Shell>} />
          <Route path="/schedules" element={<Shell><SchedulesPage /></Shell>} />
          <Route path="/settings" element={<Shell><SettingsPage /></Shell>} />
          <Route path="/audit" element={<Shell><AuditPage /></Shell>} />
          <Route path="/maintenance" element={<Shell><MaintenancePage /></Shell>} />
          <Route path="/mfa" element={<Shell><MfaPage /></Shell>} />
          <Route path="*" element={<Navigate to="/channels" replace />} />
        </Routes>
      </BrowserRouter>
    </SessionProvider>
  )
}
