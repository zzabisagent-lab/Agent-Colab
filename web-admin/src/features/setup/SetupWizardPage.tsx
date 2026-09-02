import { type FormEvent, useState } from 'react'
import { api } from '../../api/client'
import { codeOf, useResource } from '../../lib/useList'

interface SetupState { state: string; steps?: { name: string; ok: boolean; detail?: string }[]; endpoint_lock?: unknown }
const setupPost = <T,>(path: string, body: unknown, token?: string) =>
  api<T>(path, { method: 'POST', body: JSON.stringify(body), headers: token ? { 'X-Setup-Token': token } : {} })

/** Setup Wizard (§8): preflight → bootstrap in the mandated order; secrets are never re-displayed. */
export function SetupWizardPage() {
  const { data, error, reload, setError } = useResource<SetupState>('/setup/state')
  const [token, setToken] = useState('')
  const [dbUrl, setDbUrl] = useState('')
  const [masterKey, setMasterKey] = useState('')
  const [ownerId, setOwnerId] = useState('acct-owner')
  const [ownerName, setOwnerName] = useState('System Owner')
  const [mmUrl, setMmUrl] = useState('')
  const [mmToken, setMmToken] = useState('')
  const [storageRoot, setStorageRoot] = useState('')
  const [preflight, setPreflight] = useState<{ name: string; ok: boolean; detail?: string }[] | null>(null)
  const [diff, setDiff] = useState<unknown>(null)
  const [result, setResult] = useState<{ recovery_code?: string; totp_uri?: string; state?: string } | null>(null)
  const body = () => ({
    database_url: dbUrl, master_key_b64: masterKey,
    owner: { account_id: ownerId, display_name: ownerName },
    integrations: { mattermost: { url: mmUrl, bot_token: mmToken }, storage: { root: storageRoot } },
  })
  async function runPreflight(e: FormEvent) {
    e.preventDefault(); setError(null)
    try {
      const r = await setupPost<{ steps: { name: string; ok: boolean; detail?: string }[] }>('/setup/preflight', body(), token)
      setPreflight(r.steps)
      setDiff(await setupPost<unknown>('/setup/diff', body(), token))
    } catch (err) { setError(codeOf(err)) }
  }
  async function bootstrap() {
    if (!window.confirm('Apply the configuration? DB migration, key provider, Owner/TOTP, integrations, then lock.')) return
    setError(null)
    try {
      const r = await setupPost<{ recovery_code?: string; totp_uri?: string; state?: string }>('/setup/bootstrap', body(), token)
      setResult(r); setDbUrl(''); setMasterKey(''); setMmToken('')
      await reload()
    } catch (err) { setError(codeOf(err)) }
  }
  return (
    <section>
      <h1>Setup Wizard</h1>
      {error && <p role="alert" className="error">{error}</p>}
      <p>State: <strong>{data?.state ?? 'unknown'}</strong></p>
      {data?.state === 'LOCKED' ? (
        <p role="status">Setup is complete and locked. Reconfiguration requires maintenance mode, the recovery code and MFA re-authentication.</p>
      ) : (
        <form onSubmit={runPreflight} aria-labelledby="setup-form">
          <h2 id="setup-form">Configuration</h2>
          <label htmlFor="setup-token">Setup token</label>
          <input id="setup-token" type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} required />
          <label htmlFor="setup-db">Database URL</label>
          <input id="setup-db" type="password" autoComplete="off" value={dbUrl} onChange={(e) => setDbUrl(e.target.value)} required />
          <label htmlFor="setup-key">Master key (base64), kept in memory only</label>
          <input id="setup-key" type="password" autoComplete="off" value={masterKey} onChange={(e) => setMasterKey(e.target.value)} required />
          <label htmlFor="setup-owner">Owner account id</label>
          <input id="setup-owner" value={ownerId} onChange={(e) => setOwnerId(e.target.value)} required />
          <label htmlFor="setup-owner-name">Owner display name</label>
          <input id="setup-owner-name" value={ownerName} onChange={(e) => setOwnerName(e.target.value)} required />
          <label htmlFor="setup-mm-url">Mattermost URL</label>
          <input id="setup-mm-url" value={mmUrl} onChange={(e) => setMmUrl(e.target.value)} />
          <label htmlFor="setup-mm-token">Mattermost bot token</label>
          <input id="setup-mm-token" type="password" autoComplete="off" value={mmToken} onChange={(e) => setMmToken(e.target.value)} />
          <label htmlFor="setup-storage">Storage root</label>
          <input id="setup-storage" value={storageRoot} onChange={(e) => setStorageRoot(e.target.value)} />
          <button type="submit">Run preflight</button>
          <button type="button" onClick={() => void bootstrap()} disabled={!preflight || !preflight.every((s) => s.ok)}>Apply and lock</button>
        </form>
      )}
      {preflight && (
        <table>
          <caption>Preflight</caption>
          <thead><tr><th>Step</th><th>Result</th><th>Guidance</th></tr></thead>
          <tbody>{preflight.map((s) => <tr key={s.name}><td>{s.name}</td><td>{s.ok ? 'passed' : 'failed'}</td><td>{s.detail ?? '—'}</td></tr>)}</tbody>
        </table>
      )}
      {diff !== null && <section aria-label="Redacted diff"><h2>Redacted diff before apply</h2><pre>{JSON.stringify(diff, null, 2)}</pre></section>}
      {result && (
        <section aria-label="Setup result" role="status">
          <h2>Setup complete ({result.state ?? 'LOCKED'})</h2>
          {result.recovery_code && <p>Recovery code (shown once, store it now): <code>{result.recovery_code}</code></p>}
          {result.totp_uri && <p>TOTP enrollment URI (shown once): <code>{result.totp_uri}</code></p>}
        </section>
      )}
    </section>
  )
}
