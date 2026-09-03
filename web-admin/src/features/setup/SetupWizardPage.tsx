import { type FormEvent, useState } from 'react'
import { api } from '../../api/client'
import { codeOf, useResource } from '../../lib/useList'

interface Step { name: string; ok: boolean; detail?: string | null; guidance?: string | null }
interface SetupState { state: string; owner_created?: boolean; load_error?: string | null }
interface Owner { service_token?: string; totp_secret_b32?: string; otpauth_uri?: string; recovery_code?: string }
interface BootstrapResult { state: string; owner?: Owner; retry_token?: string; failed_step?: string; error_code?: string; shown_once?: boolean }

const post = <T,>(path: string, body?: unknown) =>
  api<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })

/** Setup Wizard (development plan §8): token → configure sections → preflight → diff → bootstrap.
 *  Secrets travel once to the server (15-minute in-memory handles) and are never re-displayed. */
export function SetupWizardPage() {
  const { data, error, reload, setError } = useResource<SetupState>('/setup/state')
  const [token, setToken] = useState('')
  const [dbHost, setDbHost] = useState('127.0.0.1')
  const [dbPort, setDbPort] = useState('5432')
  const [dbName, setDbName] = useState('')
  const [dbUser, setDbUser] = useState('')
  const [dbPassword, setDbPassword] = useState('')
  const [keyPath, setKeyPath] = useState('')
  const [ownerId, setOwnerId] = useState('acct-owner')
  const [ownerName, setOwnerName] = useState('System Owner')
  const [instanceName, setInstanceName] = useState('Agent-Colab')
  const [baseUrl, setBaseUrl] = useState(window.location.origin)
  const [mmUrl, setMmUrl] = useState('')
  const [mmTeam, setMmTeam] = useState('')
  const [mmToken, setMmToken] = useState('')
  const [artifactRoot, setArtifactRoot] = useState('')
  const [documentRoot, setDocumentRoot] = useState('')
  const [opsChannel, setOpsChannel] = useState('')
  const [configured, setConfigured] = useState<string[]>([])
  const [preflight, setPreflight] = useState<Step[] | null>(null)
  const [diff, setDiff] = useState<unknown>(null)
  const [result, setResult] = useState<BootstrapResult | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  async function issueToken() {
    setError(null)
    try {
      const r = await post<{ token: string; expires_in_s: number }>('/setup/token')
      setToken(r.token)
      setNotice(`SETUP_TOKEN_ISSUED (valid ${Math.round(r.expires_in_s / 60)} min, shown once)`)
    } catch (e) { setError(codeOf(e)) }
  }
  async function configure(e: FormEvent) {
    e.preventDefault()
    setError(null); setNotice(null); setPreflight(null); setDiff(null)
    const sections: [string, Record<string, unknown>][] = [
      ['db', { db_host: dbHost, db_port: Number(dbPort), db_name: dbName, db_user: dbUser, db_password: dbPassword }],
      ['keys', { 'secrets.provider': 'local', 'secrets.master_key_path': keyPath }],
      ['owner', { account_id: ownerId, display_name: ownerName }],
      ['integrations', {
        'instance.name': instanceName, 'instance.base_url': baseUrl,
        'mattermost.url': mmUrl, 'mattermost.team': mmTeam, 'mattermost.bot_token': mmToken,
        'storage.artifact_root': artifactRoot, 'storage.document_root': documentRoot, 'ops.channel_id': opsChannel,
      }],
    ]
    try {
      const done: string[] = []
      for (const [section, values] of sections) {
        await post('/setup/configure', { section, values })
        done.push(section)
      }
      setConfigured(done)
      setDbPassword(''); setMmToken('')  // secrets are handles on the server now
      const pf = await post<{ ok: boolean; steps: Step[] }>('/setup/preflight')
      setPreflight(pf.steps)
      if (pf.ok) setDiff(await api<unknown>('/setup/diff'))
      setNotice(pf.ok ? 'SETUP_PREFLIGHT_PASSED' : 'SETUP_PREFLIGHT_FAILED')
    } catch (err) { setError(codeOf(err)) }
  }
  async function bootstrap() {
    if (!window.confirm('Apply the configuration? DB migration, key provider, Owner/TOTP, integrations, then lock.')) return
    setError(null)
    try {
      const r = await post<BootstrapResult>('/setup/bootstrap', { token })
      setResult(r); setToken('')
      await reload()
    } catch (err) { setError(codeOf(err)) }
  }
  const locked = data?.state === 'LOCKED'
  return (
    <section>
      <h1>Setup Wizard</h1>
      {error && <p role="alert" className="error">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <p>State: <strong data-testid="setup-state">{data?.state ?? 'unknown'}</strong></p>
      {locked ? (
        <p>Setup is complete and locked. Reconfiguration requires maintenance mode, the recovery code and MFA re-authentication.</p>
      ) : (
        <>
          <section aria-labelledby="setup-token-h">
            <h2 id="setup-token-h">1. Setup token</h2>
            <button type="button" onClick={() => void issueToken()}>Issue setup token</button>
            <label htmlFor="setup-token">Setup token (single use, 30 minutes)</label>
            <input id="setup-token" type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} />
          </section>
          <form onSubmit={configure} aria-labelledby="setup-config-h">
            <h2 id="setup-config-h">2. Configuration</h2>
            <fieldset><legend>Database</legend>
              <label htmlFor="db-host">Host</label><input id="db-host" value={dbHost} onChange={(e) => setDbHost(e.target.value)} required />
              <label htmlFor="db-port">Port</label><input id="db-port" inputMode="numeric" value={dbPort} onChange={(e) => setDbPort(e.target.value)} required />
              <label htmlFor="db-name">Database name</label><input id="db-name" value={dbName} onChange={(e) => setDbName(e.target.value)} required />
              <label htmlFor="db-user">User</label><input id="db-user" value={dbUser} onChange={(e) => setDbUser(e.target.value)} />
              <label htmlFor="db-password">Password (kept in memory only)</label><input id="db-password" type="password" autoComplete="off" value={dbPassword} onChange={(e) => setDbPassword(e.target.value)} />
            </fieldset>
            <fieldset><legend>Keys and secret provider</legend>
              <label htmlFor="key-path">Master key file path (owner-only, outside backups)</label><input id="key-path" value={keyPath} onChange={(e) => setKeyPath(e.target.value)} required />
            </fieldset>
            <fieldset><legend>System Owner</legend>
              <label htmlFor="owner-id">Owner account id</label><input id="owner-id" value={ownerId} onChange={(e) => setOwnerId(e.target.value)} required />
              <label htmlFor="owner-name">Owner display name</label><input id="owner-name" value={ownerName} onChange={(e) => setOwnerName(e.target.value)} required />
            </fieldset>
            <fieldset><legend>Integrations</legend>
              <label htmlFor="instance-name">Instance name</label><input id="instance-name" value={instanceName} onChange={(e) => setInstanceName(e.target.value)} required />
              <label htmlFor="base-url">Base URL</label><input id="base-url" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} required />
              <label htmlFor="mm-url">Mattermost URL</label><input id="mm-url" value={mmUrl} onChange={(e) => setMmUrl(e.target.value)} required />
              <label htmlFor="mm-team">Mattermost team</label><input id="mm-team" value={mmTeam} onChange={(e) => setMmTeam(e.target.value)} required />
              <label htmlFor="mm-token">Mattermost bot token</label><input id="mm-token" type="password" autoComplete="off" value={mmToken} onChange={(e) => setMmToken(e.target.value)} required />
              <label htmlFor="artifact-root">Artifact storage root</label><input id="artifact-root" value={artifactRoot} onChange={(e) => setArtifactRoot(e.target.value)} required />
              <label htmlFor="document-root">Document storage root</label><input id="document-root" value={documentRoot} onChange={(e) => setDocumentRoot(e.target.value)} required />
              <label htmlFor="ops-channel">Ops channel id</label><input id="ops-channel" value={opsChannel} onChange={(e) => setOpsChannel(e.target.value)} required />
            </fieldset>
            <button type="submit">Save sections and run preflight</button>
          </form>
          {configured.length > 0 && <p>Configured sections: {configured.join(', ')}</p>}
          {preflight && (
            <table>
              <caption>3. Preflight</caption>
              <thead><tr><th>Step</th><th>Result</th><th>Detail / guidance</th></tr></thead>
              <tbody>{preflight.map((s) => <tr key={s.name}><td>{s.name}</td><td>{s.ok ? 'passed' : 'failed'}</td><td>{s.detail ?? s.guidance ?? '—'}</td></tr>)}</tbody>
            </table>
          )}
          {diff !== null && (
            <section aria-labelledby="setup-diff-h">
              <h2 id="setup-diff-h">4. Redacted diff before apply</h2>
              <pre>{JSON.stringify(diff, null, 2)}</pre>
              <button type="button" onClick={() => void bootstrap()} disabled={!preflight?.every((s) => s.ok) || !token}>5. Apply and lock</button>
            </section>
          )}
        </>
      )}
      {result && (
        <section aria-labelledby="setup-result-h" role="status">
          <h2 id="setup-result-h">Setup result: {result.state}</h2>
          {result.owner?.recovery_code && <p>Recovery code (shown once, store it now): <code data-testid="recovery-code">{result.owner.recovery_code}</code></p>}
          {result.owner?.otpauth_uri && <p>TOTP enrollment URI (shown once): <code>{result.owner.otpauth_uri}</code></p>}
          {result.owner?.service_token && <p>Owner service token (shown once): <code>{result.owner.service_token}</code></p>}
          {result.retry_token && <p>Bootstrap failed at {result.failed_step ?? '?'} ({result.error_code ?? '?'}); retry token: <code>{result.retry_token}</code></p>}
        </section>
      )}
    </section>
  )
}
