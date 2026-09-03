import { type FormEvent, useState } from 'react'
import { post } from '../../api/client'
import { codeOf, useResource } from '../../lib/useList'

interface SecretMeta { secret_ref: string; name: string; version: number; provider: string; created_at?: string; grants?: Grant[] }
interface Grant { grant_id: string; agent_id: string; task_id?: string | null; action?: string | null; revoked_at?: string | null }
const base = '/api/v1/secrets'

export function SecretsPage() {
  const { data, error, reload, setError } = useResource<{ items: SecretMeta[] }>(base)
  const [notice, setNotice] = useState<string | null>(null)
  const [ref, setRef] = useState('')
  const [agent, setAgent] = useState('')
  const [task, setTask] = useState('')
  async function run(action: () => Promise<unknown>, ok: string) {
    setError(null); setNotice(null)
    try { await action(); setNotice(ok); await reload() } catch (e) { setError(codeOf(e)) }
  }
  function grant(e: FormEvent) {
    e.preventDefault()
    void run(() => post(`${base}/${ref}/grants`, { agent_id: agent, task_id: task || null }), 'SECRET_GRANT_CREATED')
  }
  return (
    <section>
      <h1>Secrets</h1>
      <p>Values are never shown; this screen lists metadata, grants and audit only.</p>
      {error && <p role="alert" className="error">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <form onSubmit={grant} aria-labelledby="grant-secret">
        <h2 id="grant-secret">Grant a secret to an Agent</h2>
        <label htmlFor="secret-ref">Secret reference</label>
        <input id="secret-ref" value={ref} onChange={(e) => setRef(e.target.value)} required />
        <label htmlFor="grant-agent">Agent id</label>
        <input id="grant-agent" value={agent} onChange={(e) => setAgent(e.target.value)} required />
        <label htmlFor="grant-task">Task id, optional</label>
        <input id="grant-task" value={task} onChange={(e) => setTask(e.target.value)} />
        <button type="submit">Grant</button>
      </form>
      <table>
        <caption>Secrets (metadata)</caption>
        <thead><tr><th>Reference</th><th>Name</th><th>Version</th><th>Provider</th><th>Grants</th></tr></thead>
        <tbody>
          {(data?.items ?? []).map((s) => (
            <tr key={s.secret_ref}>
              <td><code>{s.secret_ref}</code></td><td>{s.name}</td><td>{s.version}</td><td>{s.provider}</td>
              <td>
                {(s.grants ?? []).map((g) => (
                  <span key={g.grant_id}>{g.agent_id}{g.task_id ? ` / ${g.task_id}` : ''}{g.revoked_at ? ' (revoked)' : ''}{' '}
                    {!g.revoked_at && <button onClick={() => void run(() => post(`${base}/grants/${g.grant_id}/revoke`, {}), 'SECRET_GRANT_REVOKED')}>Revoke</button>}
                  </span>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
