import { type FormEvent, useState } from 'react'
import { post } from '../../api/client'
import { codeOf, useResource } from '../../lib/useList'

interface MfaState { enrolled: boolean; confirmed: boolean; required: boolean; verified_at?: string | null; method?: string | null }
const base = '/api/v1/auth/mfa'

/** TOTP enrollment/confirmation and re-authentication (P4-09). Secrets are shown exactly once. */
export function MfaPage() {
  const { data, error, reload, setError } = useResource<MfaState>(base)
  const [notice, setNotice] = useState<string | null>(null)
  const [enrollment, setEnrollment] = useState<{ otpauth_uri: string; recovery_codes: string[] } | null>(null)
  const [code, setCode] = useState('')
  async function run(action: () => Promise<unknown>, ok: string) {
    setError(null); setNotice(null)
    try { await action(); setNotice(ok); await reload() } catch (e) { setError(codeOf(e)) }
  }
  async function enroll() {
    setError(null)
    try { setEnrollment(await post(`${base}/enroll`, {})); await reload() } catch (e) { setError(codeOf(e)) }
  }
  function confirm(e: FormEvent) { e.preventDefault(); void run(() => post(`${base}/confirm`, { code }), 'MFA_CONFIRMED') }
  function verify(e: FormEvent) { e.preventDefault(); void run(() => post(`${base}/verify`, { code }), 'MFA_VERIFIED') }
  return (
    <section>
      <h1>Multi-factor authentication</h1>
      {error && <p role="alert" className="error">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <p>Status: {data?.confirmed ? 'enrolled and confirmed' : data?.enrolled ? 'enrolled, not confirmed' : 'not enrolled'}{data?.required ? ' (required for your role)' : ''}; last verified: {data?.verified_at ?? '—'}</p>
      {!data?.enrolled && <button onClick={() => void enroll()}>Enroll TOTP</button>}
      {enrollment && (
        <section aria-label="Enrollment secret" role="status">
          <h2>Add this to your authenticator now (shown once)</h2>
          <p><code>{enrollment.otpauth_uri}</code></p>
          <p>Recovery codes (shown once): {enrollment.recovery_codes.join(' ')}</p>
        </section>
      )}
      <form onSubmit={data?.confirmed ? verify : confirm} aria-labelledby="mfa-code-form">
        <h2 id="mfa-code-form">{data?.confirmed ? 'Re-authenticate' : 'Confirm enrollment'}</h2>
        <label htmlFor="mfa-code">Authenticator code</label>
        <input id="mfa-code" inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(e) => setCode(e.target.value)} required />
        <button type="submit">{data?.confirmed ? 'Verify' : 'Confirm'}</button>
      </form>
    </section>
  )
}
