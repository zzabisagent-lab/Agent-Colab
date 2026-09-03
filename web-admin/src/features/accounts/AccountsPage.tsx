import { type FormEvent, useState } from 'react'
import { post } from '../../api/client'
import { codeOf, useResource } from '../../lib/useList'

interface Account { account_id: string; account_type: string; display_name: string; status: string; roles?: string[] }
const base = '/api/v1/accounts'

export function AccountsPage() {
  const { data, error, reload, setError } = useResource<{ items: Account[] }>(base)
  const [notice, setNotice] = useState<string | null>(null)
  const [accountId, setAccountId] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [type, setType] = useState('human')
  async function run(action: () => Promise<unknown>, ok: string) {
    setError(null); setNotice(null)
    try { await action(); setNotice(ok); await reload() } catch (e) { setError(codeOf(e)) }
  }
  function create(e: FormEvent) {
    e.preventDefault()
    void run(() => post(base, { account_id: accountId, display_name: displayName, account_type: type }), 'ACCOUNT_CREATED')
  }
  return (
    <section>
      <h1>Accounts</h1>
      {error && <p role="alert" className="error">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <form onSubmit={create} aria-labelledby="create-account">
        <h2 id="create-account">Create Account</h2>
        <label htmlFor="account-id">Account id</label>
        <input id="account-id" value={accountId} onChange={(e) => setAccountId(e.target.value)} required />
        <label htmlFor="account-name">Display name</label>
        <input id="account-name" value={displayName} onChange={(e) => setDisplayName(e.target.value)} required />
        <label htmlFor="account-type">Account type</label>
        <select id="account-type" value={type} onChange={(e) => setType(e.target.value)}>
          <option value="human">human</option><option value="service">service</option>
        </select>
        <button type="submit">Create</button>
      </form>
      <table>
        <caption>Accounts</caption>
        <thead><tr><th>Account</th><th>Type</th><th>Name</th><th>Status</th><th>Roles</th><th>Actions</th></tr></thead>
        <tbody>
          {(data?.items ?? []).map((a) => (
            <tr key={a.account_id}>
              <td><code>{a.account_id}</code></td><td>{a.account_type}</td><td>{a.display_name}</td><td>{a.status}</td>
              <td>{(a.roles ?? []).join(', ') || '—'}</td>
              <td>
                <button onClick={() => void run(() => post(`${base}/${a.account_id}/suspend`, {}), 'ACCOUNT_SUSPENDED')}>Suspend</button>
                <button onClick={() => void run(() => post(`${base}/${a.account_id}/reinstate`, {}), 'ACCOUNT_REINSTATED')}>Reinstate</button>
                <button onClick={() => { if (window.confirm(`Request hard deletion of ${a.account_id}? This starts the dual-approval workflow.`)) void run(() => post(`${base}/${a.account_id}/deletion-requests`, { reason: 'console request' }), 'ACCOUNT_DELETION_REQUESTED') }}>Request deletion</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
