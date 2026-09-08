import { useState } from 'react'
import { ApiError, get } from '../../api/client'

export function ProviderConnections() {
  const [guide, setGuide] = useState<unknown>(null)
  const [error, setError] = useState<string | null>(null)
  async function load() {
    try {
      setError(null)
      setGuide(await get('/api/v1/providers/connection-instructions'))
    } catch (e) {
      setError(e instanceof ApiError ? e.problem.code : 'Connection guide unavailable')
    }
  }
  return <aside>
    <h2>Connect Mattermost and Telegram</h2>
    <button onClick={() => void load()}>Show connection instructions</button>
    {error && <p role="alert">{error}</p>}
    {guide !== null && <pre>{JSON.stringify(guide, null, 2)}</pre>}
  </aside>
}
