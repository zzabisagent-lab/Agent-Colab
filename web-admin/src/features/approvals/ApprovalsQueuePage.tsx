import { useState } from 'react'
import { post } from '../../api/client'
import { codeOf, useResource } from '../../lib/useList'

interface QueueItem { approval_id: string; task_id?: string | null; risk: string; action: string; requested_by: string; quorum_required: number; quorum_current: number; escalated?: boolean; expires_at?: string | null }
const base = '/api/v1/approvals'

export function ApprovalsQueuePage() {
  const { data, error, reload, setError } = useResource<{ items: QueueItem[] }>(`${base}/queue`)
  const [notice, setNotice] = useState<string | null>(null)
  const [totp, setTotp] = useState('')
  async function run(action: () => Promise<unknown>, ok: string) {
    setError(null); setNotice(null)
    try { await action(); setNotice(ok); await reload() } catch (e) { setError(codeOf(e)) }
  }
  function decide(item: QueueItem, decision: 'APPROVED' | 'REJECTED') {
    const critical = item.risk === 'HIGH' || item.risk === 'CRITICAL'
    void run(async () => {
      if (critical) await post('/api/v1/auth/mfa/verify', { code: totp })  // re-authentication for HIGH and above
      await post(`${base}/${item.approval_id}/decide`, { decision, reason: 'console decision' })
    }, `APPROVAL_${decision}`)
  }
  return (
    <section>
      <h1>Approvals queue</h1>
      {error && <p role="alert" className="error">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <label htmlFor="reauth-totp">MFA code (required for HIGH and CRITICAL decisions)</label>
      <input id="reauth-totp" inputMode="numeric" autoComplete="one-time-code" value={totp} onChange={(e) => setTotp(e.target.value)} />
      <table>
        <caption>Pending approvals</caption>
        <thead><tr><th>Approval</th><th>Task</th><th>Risk</th><th>Action</th><th>Requested by</th><th>Quorum</th><th>Decision</th></tr></thead>
        <tbody>
          {(data?.items ?? []).map((i) => (
            <tr key={i.approval_id}>
              <td><code>{i.approval_id}</code></td><td>{i.task_id ?? '—'}</td><td>{i.risk}</td><td>{i.action}</td><td>{i.requested_by}</td>
              <td>{i.quorum_current} / {i.quorum_required}{i.escalated ? ' (escalated)' : ''}</td>
              <td>
                <button onClick={() => decide(i, 'APPROVED')}>Approve</button>
                <button onClick={() => decide(i, 'REJECTED')}>Reject</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
