import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, get } from '../../api/client'

export interface Channel {
  channel_id: string
  display_name: string
  channel_type: string
  status: string
  language?: string | null
}

export function ChannelsPage() {
  const [items, setItems] = useState<Channel[]>([])
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    get<{ items: Channel[] }>('/api/v1/channels')
      .then((r) => setItems(r.items ?? []))
      .catch((e) => setError(e instanceof ApiError ? e.problem.code : 'LOAD_FAILED'))
  }, [])
  return (
    <main className="page">
      <h1>Channels</h1>
      {error && <p role="alert" className="error">{error}</p>}
      <table>
        <caption>Imported Mattermost channels</caption>
        <thead><tr><th scope="col">Channel</th><th scope="col">Type</th><th scope="col">Status</th><th scope="col">Bridges</th></tr></thead>
        <tbody>
          {items.map((c) => (
            <tr key={c.channel_id}>
              <td>{c.display_name}</td><td>{c.channel_type}</td><td>{c.status}</td>
              <td><Link to={`/channels/${c.channel_id}/bridges`}>Telegram Bridges</Link></td>
            </tr>
          ))}
        </tbody>
      </table>
    </main>
  )
}
