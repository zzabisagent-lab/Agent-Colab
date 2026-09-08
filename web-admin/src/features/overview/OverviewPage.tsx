import { useResource } from '../../lib/useList'

interface Dependency { name: string; ok: boolean | null; detail?: string; checked_at?: string }
type CounterValue = number | string | boolean | null | Record<string, unknown>
interface Overview {
  dependencies: Dependency[]
  tasks: Record<string, CounterValue>
  agents: Record<string, CounterValue>
  outbox: Record<string, { pending?: number; dead?: number }>
  last_backup?: string | null
  maintenance?: { active: boolean; reason?: string } | null
  alerts?: string[]
}

function renderCounterValue(value: CounterValue): string | number {
  if (value === null) return '—'
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (typeof value === 'object') return JSON.stringify(value)
  return value
}

export function OverviewPage() {
  const { data, error, reload } = useResource<Overview>('/api/v1/ops/overview')
  return (
    <section>
      <h1>Overview</h1>
      {error && <p role="alert" className="error">{error}</p>}
      <button onClick={() => void reload()}>Refresh</button>
      {data?.alerts && data.alerts.length > 0 && (
        <section aria-label="Alerts"><h2>Alerts</h2><ul>{data.alerts.map((a) => <li key={a}>{a}</li>)}</ul></section>
      )}
      <table>
        <caption>Dependencies</caption>
        <thead><tr><th>Dependency</th><th>Status</th><th>Detail</th><th>Checked</th></tr></thead>
        <tbody>
          {(data?.dependencies ?? []).map((d) => (
            <tr key={d.name}><td>{d.name}</td><td>{d.ok ? 'ok' : 'failing'}</td><td>{d.detail ?? '—'}</td><td>{d.checked_at ?? '—'}</td></tr>
          ))}
        </tbody>
      </table>
      <section aria-label="Counters">
        <h2>Tasks</h2>
        <dl>{Object.entries(data?.tasks ?? {}).map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{renderCounterValue(v)}</dd></div>)}</dl>
        <h2>Agents</h2>
        <dl>{Object.entries(data?.agents ?? {}).map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{renderCounterValue(v)}</dd></div>)}</dl>
        <h2>Outbox</h2>
        <dl>{Object.entries(data?.outbox ?? {}).map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v.pending ?? 0} pending · {v.dead ?? 0} dead</dd></div>)}</dl>
        <p>Last backup: {data?.last_backup ?? '—'} · Maintenance: {data?.maintenance?.active ? `on (${data.maintenance.reason ?? ''})` : 'off'}</p>
      </section>
    </section>
  )
}
