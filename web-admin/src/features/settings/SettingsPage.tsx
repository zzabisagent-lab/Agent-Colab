import { useState } from 'react'
import { post, put } from '../../api/client'
import { codeOf, useResource } from '../../lib/useList'

interface Setting { key: string; scope: string; type: string; value: unknown; secret: boolean; version: number; restart_required: boolean; changed_by?: string | null }
const base = '/api/v1/settings'

export function SettingsPage() {
  const { data, error, reload, setError } = useResource<{ items: Setting[] }>(base)
  const [notice, setNotice] = useState<string | null>(null)
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  async function run(action: () => Promise<unknown>, ok: string) {
    setError(null); setNotice(null)
    try { await action(); setNotice(ok); await reload() } catch (e) { setError(codeOf(e)) }
  }
  function apply(s: Setting) {
    const raw = drafts[s.key] ?? ''
    let value: unknown = raw
    if (s.type === 'int') value = Number(raw)
    if (s.type === 'bool') value = raw === 'true'
    if (s.type === 'json') { try { value = JSON.parse(raw) } catch { setError('SETTING_VALUE_INVALID'); return } }
    void run(() => put(`${base}/${s.key}`, { value }), 'SETTING_APPLIED')
  }
  return (
    <section>
      <h1>Settings</h1>
      {error && <p role="alert" className="error">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <table>
        <caption>Runtime settings (secret values are never re-displayed)</caption>
        <thead><tr><th>Key</th><th>Scope</th><th>Type</th><th>Current</th><th>Version</th><th>New value</th><th>Actions</th></tr></thead>
        <tbody>
          {(data?.items ?? []).map((s) => (
            <tr key={s.key}>
              <td><code>{s.key}</code></td><td>{s.scope}</td><td>{s.type}{s.restart_required ? ' (restart)' : ''}</td>
              <td>{s.secret ? '••••• (secret)' : JSON.stringify(s.value)}</td><td>{s.version}</td>
              <td>
                <label htmlFor={`set-${s.key}`} className="visually-hidden">New value for {s.key}</label>
                <input id={`set-${s.key}`} type={s.secret ? 'password' : 'text'} autoComplete="off" value={drafts[s.key] ?? ''} onChange={(e) => setDrafts({ ...drafts, [s.key]: e.target.value })} />
              </td>
              <td>
                <button onClick={() => apply(s)}>Apply</button>
                <button onClick={() => { if (s.version > 1 && window.confirm(`Roll back ${s.key} to version ${s.version - 1}?`)) void run(() => post(`${base}/${s.key}/rollback/${s.version - 1}`, {}), 'SETTING_ROLLED_BACK') }} disabled={s.version <= 1}>Roll back</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
