import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError } from '../../api/client'
import { useSession } from './session'

export function LoginPage() {
  const { login } = useSession()
  const navigate = useNavigate()
  const [token, setToken] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await login(token)
      navigate('/channels')
    } catch (err) {
      setError(err instanceof ApiError ? err.problem.code : 'LOGIN_FAILED')
    } finally {
      setBusy(false)
    }
  }
  return (
    <main className="page">
      <h1>Agent-Colab Admin</h1>
      <form onSubmit={onSubmit} aria-labelledby="login-heading">
        <h2 id="login-heading">Sign in</h2>
        <label htmlFor="service-token">Service token</label>
        <input id="service-token" type="password" value={token} autoComplete="off"
               onChange={(e) => setToken(e.target.value)} required minLength={16} />
        <button type="submit" disabled={busy}>Sign in</button>
        {error && <p role="alert" className="error">{error}</p>}
      </form>
    </main>
  )
}
