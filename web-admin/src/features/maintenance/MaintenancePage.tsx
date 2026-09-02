import { useState } from 'react'
import { post } from '../../api/client'
import { codeOf, useResource } from '../../lib/useList'

interface Mode { active: boolean; reason?: string | null; entered_by?: string | null; entered_at?: string | null }
const base = '/api/v1/maintenance'

export function MaintenancePage() {
  const { data, error, reload, setError } = useResource<Mode>(base)
  const [notice, setNotice] = useState<string | null>(null)
  const [reason, setReason] = useState('')
  const [totp, setTotp] = useState('')
  async function run(action: () => Promise<unknown>, ok: string) {
    setError(null); setNotice(null)
    try { await action(); setNotice(ok); await reload() } catch (e) { setError(codeOf(e)) }
  }
  function toggle(enter: boolean) {
    const verb = enter ? 'Enter' : 'Exit'
    if (!window.confirm(`${verb} maintenance mode? Non-administrator writes will ${enter ? 'be refused with 503' : 'resume'}.`)) return
    void run(async () => {
      await post('/api/v1/auth/mfa/verify', { code: totp })
      await post(`${base}/${enter ? 'enter' : 'exit'}`, { reason })
    }, enter ? 'MAINTENANCE_ENTERED' : 'MAINTENANCE_EXITED')
  }
  return (
    <section>
      <h1>Maintenance</h1>
      {error && <p role="alert" className="error">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <p>Status: {data?.active ? `on since ${data.entered_at ?? '?'} by ${data.entered_by ?? '?'} (${data.reason ?? ''})` : 'off'}</p>
      <label htmlFor="maint-reason">Reason</label>
      <input id="maint-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
      <label htmlFor="maint-totp">MFA code</label>
      <input id="maint-totp" inputMode="numeric" autoComplete="one-time-code" value={totp} onChange={(e) => setTotp(e.target.value)} />
      <button onClick={() => toggle(true)} disabled={!!data?.active}>Enter maintenance mode</button>
      <button onClick={() => toggle(false)} disabled={!data?.active}>Exit maintenance mode</button>
    </section>
  )
}
