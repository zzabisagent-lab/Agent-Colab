import { type FormEvent, useState } from 'react'
import { useResource } from '../../lib/useList'

interface AuditRow { audit_id: string; action: string; result: string; actor_label: string; target_type: string; target_id: string; recorded_at?: string; error_code?: string | null }

export function AuditPage() {
  const [actor, setActor] = useState('')
  const [action, setAction] = useState('')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [query, setQuery] = useState('/api/v1/audit?limit=50')
  const { data, error } = useResource<{ items: AuditRow[]; next_cursor?: string | null }>(query)
  function search(e: FormEvent) {
    e.preventDefault()
    const p = new URLSearchParams({ limit: '50' })
    if (actor) p.set('actor', actor)
    if (action) p.set('action', action)
    if (from) p.set('from', from)
    if (to) p.set('to', to)
    setQuery(`/api/v1/audit?${p.toString()}`)
  }
  const exportHref = query.replace('/api/v1/audit?', '/api/v1/audit/export?format=csv&')
  return (
    <section>
      <h1>Audit Explorer</h1>
      {error && <p role="alert" className="error">{error}</p>}
      <form onSubmit={search} aria-labelledby="audit-search">
        <h2 id="audit-search">Search</h2>
        <label htmlFor="audit-actor">Actor</label>
        <input id="audit-actor" value={actor} onChange={(e) => setActor(e.target.value)} />
        <label htmlFor="audit-action">Action</label>
        <input id="audit-action" value={action} onChange={(e) => setAction(e.target.value)} />
        <label htmlFor="audit-from">From (ISO)</label>
        <input id="audit-from" value={from} onChange={(e) => setFrom(e.target.value)} />
        <label htmlFor="audit-to">To (ISO)</label>
        <input id="audit-to" value={to} onChange={(e) => setTo(e.target.value)} />
        <button type="submit">Search</button>
        <a href={exportHref}>Export CSV</a>
      </form>
      <table>
        <caption>Audit events (secret metadata stays redacted)</caption>
        <thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>Result</th><th>Error</th></tr></thead>
        <tbody>
          {(data?.items ?? []).map((r) => (
            <tr key={r.audit_id}><td>{r.recorded_at ?? '—'}</td><td>{r.actor_label}</td><td>{r.action}</td><td>{r.target_type}/{r.target_id}</td><td>{r.result}</td><td>{r.error_code ?? '—'}</td></tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
