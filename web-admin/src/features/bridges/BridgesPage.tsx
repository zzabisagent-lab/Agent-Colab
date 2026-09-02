import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useParams } from 'react-router-dom'
import { ApiError, get, post } from '../../api/client'

export interface Bridge {
  bridge_id: string
  channel_id: string
  provider_instance_id: string
  telegram_chat_id: string
  telegram_thread_id?: number | null
  thread_mode: string
  direction: string
  status: string
  admin_exception: boolean
  allow_commands: boolean
}

export function BridgesPage() {
  const { channelId } = useParams()
  const base = `/api/v1/channels/${channelId}/bridges`
  const [items, setItems] = useState<Bridge[]>([])
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [instance, setInstance] = useState('')
  const [chat, setChat] = useState('')
  const [thread, setThread] = useState('')
  const [direction, setDirection] = useState('bidirectional')
  const reload = useCallback(() => {
    get<{ items: Bridge[] }>(base).then((r) => setItems(r.items ?? [])).catch((e) => setError(e instanceof ApiError ? e.problem.code : 'LOAD_FAILED'))
  }, [base])
  useEffect(reload, [reload])
  async function run(action: () => Promise<unknown>, ok: string) {
    setError(null); setNotice(null)
    try { await action(); setNotice(ok); reload() } catch (e) { setError(e instanceof ApiError ? e.problem.code : 'ACTION_FAILED') }
  }
  function create(e: FormEvent) {
    e.preventDefault()
    void run(
      () => post(base, {
        provider_instance_id: instance,
        telegram_chat_id: chat,
        telegram_thread_id: thread ? Number(thread) : null,
        direction,
      }),
      'BRIDGE_CREATED',
    )
  }
  return (
    <main className="page">
      <h1>Telegram Bridges</h1>
      <p>Channel <code>{channelId}</code></p>
      {error && <p role="alert" className="error">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <form onSubmit={create} aria-labelledby="create-bridge">
        <h2 id="create-bridge">Add Bridge</h2>
        <label htmlFor="instance">Telegram provider instance id</label>
        <input id="instance" value={instance} onChange={(e) => setInstance(e.target.value)} required />
        <label htmlFor="chat">Telegram chat id</label>
        <input id="chat" value={chat} onChange={(e) => setChat(e.target.value)} required />
        <label htmlFor="thread">Topic (thread) id, optional</label>
        <input id="thread" inputMode="numeric" value={thread} onChange={(e) => setThread(e.target.value)} />
        <label htmlFor="direction">Direction</label>
        <select id="direction" value={direction} onChange={(e) => setDirection(e.target.value)}>
          <option value="bidirectional">bidirectional</option>
          <option value="mattermost_to_telegram">Mattermost → Telegram</option>
          <option value="telegram_to_mattermost">Telegram → Mattermost</option>
        </select>
        <button type="submit">Create</button>
      </form>
      <table>
        <caption>Bridges of this channel</caption>
        <thead><tr><th scope="col">Bridge</th><th scope="col">Target</th><th scope="col">Direction</th><th scope="col">Status</th><th scope="col">Actions</th></tr></thead>
        <tbody>
          {items.map((b) => (
            <tr key={b.bridge_id}>
              <td>{b.bridge_id}</td>
              <td>{b.telegram_chat_id}{b.telegram_thread_id ? `:${b.telegram_thread_id}` : ''}</td>
              <td>{b.direction}</td><td>{b.status}</td>
              <td>
                <button onClick={() => void run(() => post(`${base}/${b.bridge_id}/test`), 'BRIDGE_TEST_SENT')}>Test</button>
                <button onClick={() => void run(() => post(`${base}/${b.bridge_id}/enable`), 'BRIDGE_ENABLED')}>Enable</button>
                <button onClick={() => void run(() => post(`${base}/${b.bridge_id}/disable`), 'BRIDGE_DISABLED')}>Disable</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </main>
  )
}
